"""Serve `@agent` functions over the RunWeave HTTP agent contract (`runweave.agent/v1`).

    from runweave.sdk_http import create_agent_app
    app = create_agent_app(my_agent)          # any ASGI server, or:
    runweave serve-agent mypkg.agent:run      # loopback only

The same `RunContext` is available as for in-process agents: run ID, attempt, the
remaining deadline sent by RunWeave, timeline steps (returned with the output) and
redacted logging. Usage is None until the LLM proxy (milestone 21) meters model calls.
`ctx.request_approval` calls back to the RunWeave that sent the run (durable mode only).
"""

import asyncio
import logging
import socket
from collections import OrderedDict
from collections.abc import Callable, Sequence
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import httpx
import uvicorn
from pydantic import BaseModel, ValidationError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from runweave.action_approvals import (
    Approval,
    ApprovalExpired,
    ApprovalRefused,
    ApprovalUnavailable,
    approval_scope,
)
from runweave.agent_effects import effect_faults_scope
from runweave.deadline import RUN_DEADLINE, DeadlineExceeded, deadline_scope, paused_deadline
from runweave.http_contract import (
    CONTRACT_VERSION,
    MANIFEST_PATH,
    AgentDescriptor,
    Manifest,
    RunRequest,
    StepRecord,
)
from runweave.sdk import (
    AgentError,
    AgentFunction,
    AgentSpec,
    ModelProxy,
    RunContext,
    validation_errors,
)
from runweave.security import ContentPolicy
from runweave.steps import STEP_EVENT_CAP, StepEvent, step_scope

READY_PREFIX = "RUNWEAVE_AGENT_READY"
APPROVAL_WAIT_SECONDS = 20
_logger = logging.getLogger("runweave.agent")


def _error(status: int, code: str, **extra: Any) -> JSONResponse:
    headers = {"Retry-After": "1"} if status == 503 else None
    return JSONResponse({"error": {"code": code, **extra}}, status_code=status, headers=headers)


class _CollectedSteps:
    """Step log for one run, returned to RunWeave with the output."""

    def __init__(self) -> None:
        self._events: list[StepEvent] = []

    async def begin(self, title: str, detail: str | None) -> StepEvent | None:
        if len(self._events) >= STEP_EVENT_CAP:
            return None
        event = StepEvent(
            index=len(self._events),
            title=title,
            detail=detail,
            status="running",
            started_at=datetime.now(UTC),
        )
        self._events.append(event)
        return event

    async def end(self, event: StepEvent) -> None:
        self._events[event.index] = event

    def records(self) -> list[dict[str, Any]]:
        effect = ("effect", "operation", "resource", "fault")
        return [
            StepRecord.model_validate(
                {
                    "title": event.title,
                    "detail": event.detail,
                    "status": "succeeded" if event.status == "succeeded" else "failed",
                    "duration_ms": event.duration_ms or 0,
                    **(
                        {"kind": "effect", **{key: getattr(event, key) for key in effect}}
                        if event.kind == "effect"
                        else {}
                    ),
                }
            ).model_dump(mode="json")
            for event in self._events
        ]


class _CallbackApprovals:
    """`ctx.request_approval` for an agent served over HTTP: ask the RunWeave that sent the
    run, repeating the (idempotent) call until there is a decision. The agent's own deadline
    is paused while it waits, as RunWeave pauses the run's."""

    def __init__(self, url: str, token: str) -> None:
        self._url = url
        self._headers = {"Authorization": f"Bearer {token}"}

    async def request(self, action: str, arguments: Any, expires_in: float | None) -> Approval:
        body = {
            "action": action,
            "arguments": arguments,
            "expires_in": expires_in,
            "wait_seconds": APPROVAL_WAIT_SECONDS,
        }
        failures = 0
        loop = asyncio.get_running_loop()
        async with (
            paused_deadline(loop.time() + 86400),  # RunWeave bounds the wait itself
            httpx.AsyncClient(trust_env=False, timeout=APPROVAL_WAIT_SECONDS + 15) as client,
        ):
            while True:
                try:
                    response = await client.post(self._url, json=body, headers=self._headers)
                except httpx.TransportError:
                    # RunWeave is unreachable (restarting, say): keep trying. The run cannot
                    # progress without it, and RunWeave cancels this run if it gives up.
                    failures += 1
                    await asyncio.sleep(min(2.0, 0.2 * 2 ** min(failures, 4)))
                    continue
                failures = 0
                if response.status_code != 200:
                    raise _refused(response)
                approval = response.json()["approval"]
                if approval["status"] != "pending":
                    break
        if approval["status"] in ("approved", "rejected"):
            decided = approval.get("decided_at")
            return Approval(
                id=UUID(approval["id"]),
                status=approval["status"],
                decided_by=approval.get("decided_by"),
                decided_at=datetime.fromisoformat(decided) if decided else None,
            )
        if approval["status"] == "expired":
            raise ApprovalExpired()
        raise ApprovalRefused("APPROVAL_CANCELLED")


def _refused(response: httpx.Response) -> AgentError:
    try:
        code = response.json()["error"]["code"]
    except (ValueError, KeyError, TypeError):
        code = None
    if code == ApprovalUnavailable.code:
        return ApprovalUnavailable()
    return ApprovalRefused(code if isinstance(code, str) else "APPROVAL_CALLBACK_FAILED")


@dataclass
class _Execution:
    name: str
    run_id: UUID
    task: "asyncio.Task[tuple[int, dict[str, Any]]]"
    waiters: int = field(default=0)


class AgentServer:
    def __init__(
        self,
        functions: Sequence[AgentFunction],
        *,
        max_concurrency: int = 32,
        max_body_bytes: int = 1_048_576,
        protected_values: Sequence[str] = (),
        idempotency_cache_size: int = 256,
        idempotency_ttl_seconds: float = 600,
    ) -> None:
        if not functions:
            raise ValueError("Serve at least one agent")
        self._agents: dict[str, tuple[AgentSpec, AgentFunction]] = {}
        for function in functions:
            spec = getattr(function, "__runweave_agent__", None)
            if not isinstance(spec, AgentSpec):
                raise TypeError("Agents must be decorated with @runweave.sdk.agent")
            if spec.name in self._agents:
                raise ValueError(f"Agent name {spec.name!r} is served more than once")
            self._agents[spec.name] = (spec, function)
        if not 1 <= max_concurrency <= 1024:
            raise ValueError("max_concurrency must be between 1 and 1024")
        self._max_concurrency = max_concurrency
        self._max_body = max_body_bytes
        self._policy = ContentPolicy(tuple(protected_values))
        self._running: dict[str, _Execution] = {}
        self._done: OrderedDict[str, tuple[float, str, UUID, int, dict[str, Any]]] = OrderedDict()
        self._cache_size = idempotency_cache_size
        self._cache_ttl = idempotency_ttl_seconds
        self._anonymous = 0
        self.app = Starlette(
            routes=[
                Route(MANIFEST_PATH, self.manifest, methods=["GET"]),
                Route(MANIFEST_PATH + "/{name}/runs", self.run, methods=["POST"]),
                Route(
                    MANIFEST_PATH + "/{name}/runs/{run_id}/cancel", self.cancel, methods=["POST"]
                ),
            ]
        )
        self.app.state.agent_server = self

    async def manifest(self, request: Request) -> Response:
        body = Manifest(
            contract=CONTRACT_VERSION,
            agents=[
                AgentDescriptor(
                    name=spec.name,
                    version=spec.version,
                    description=spec.description,
                    input_schema=spec.input_schema(),
                    output_schema=spec.output_schema(),
                    max_steps=spec.max_steps,
                )
                for spec, _ in self._agents.values()
            ],
        )
        return JSONResponse(body.model_dump(mode="json"))

    async def run(self, request: Request) -> Response:
        entry = self._agents.get(request.path_params["name"])
        if entry is None:
            return _error(404, "UNKNOWN_AGENT")
        spec, function = entry
        body = await self._body(request)
        if body is None:
            return _error(413, "INPUT_TOO_LARGE")
        try:
            envelope = RunRequest.model_validate_json(body)
        except ValidationError:
            return _error(400, "INVALID_REQUEST")
        try:
            parsed = spec.input_model.model_validate(envelope.input)
        except ValidationError as error:
            return _error(422, "INVALID_INPUT", errors=validation_errors(error))
        key = request.headers.get("idempotency-key")
        if key is not None and not (0 < len(key) <= 200 and key.isprintable()):
            return _error(400, "INVALID_REQUEST")

        self._prune()
        if key is not None and key in self._done:
            _, name, run_id, status, payload = self._done[key]
            if (name, run_id) != (spec.name, envelope.run_id):
                return _error(409, "IDEMPOTENCY_CONFLICT")
            return JSONResponse(payload, status_code=status)
        execution = self._running.get(key) if key is not None else None
        if execution is not None and (execution.name, execution.run_id) != (
            spec.name,
            envelope.run_id,
        ):
            return _error(409, "IDEMPOTENCY_CONFLICT")
        if execution is None:
            if len(self._running) >= self._max_concurrency:
                return _error(503, "AGENT_BUSY")
            if key is None:
                self._anonymous += 1
                key = f"anonymous:{self._anonymous}"
            execution = self._start(key, spec, function, envelope, parsed)
        return await self._wait(request, execution)

    async def cancel(self, request: Request) -> Response:
        try:
            run_id = UUID(request.path_params["run_id"])
        except ValueError:
            return _error(404, "UNKNOWN_RUN")
        name = request.path_params["name"]
        matches = [e for e in self._running.values() if (e.name, e.run_id) == (name, run_id)]
        for execution in matches:
            execution.task.cancel()
        return JSONResponse({}, status_code=202) if matches else _error(404, "UNKNOWN_RUN")

    def _start(
        self,
        key: str,
        spec: AgentSpec,
        function: AgentFunction,
        envelope: RunRequest,
        parsed: BaseModel,
    ) -> _Execution:
        task = asyncio.create_task(self._execute(spec, function, envelope, parsed))
        execution = _Execution(spec.name, envelope.run_id, task)
        self._running[key] = execution

        def finished(done: "asyncio.Task[tuple[int, dict[str, Any]]]") -> None:
            self._running.pop(key, None)
            if done.cancelled() or done.exception() is not None or key.startswith("anonymous:"):
                return
            status, payload = done.result()
            loop = asyncio.get_running_loop()
            self._done[key] = (
                loop.time() + self._cache_ttl,
                spec.name,
                envelope.run_id,
                status,
                payload,
            )
            while len(self._done) > self._cache_size:
                self._done.popitem(last=False)

        task.add_done_callback(finished)
        return execution

    async def _wait(self, request: Request, execution: _Execution) -> Response:
        execution.waiters += 1
        disconnected = asyncio.create_task(_disconnect(request))
        try:
            await asyncio.wait({execution.task, disconnected}, return_when=asyncio.FIRST_COMPLETED)
        except asyncio.CancelledError:
            if execution.waiters == 1:
                execution.task.cancel()
            raise
        finally:
            execution.waiters -= 1
            disconnected.cancel()
        if not execution.task.done():
            # RunWeave closed the request (cancellation or deadline); stop unless others wait.
            if execution.waiters == 0:
                execution.task.cancel()
            return _error(409, "RUN_CANCELLED")
        if execution.task.cancelled():
            return _error(409, "RUN_CANCELLED")
        status, payload = execution.task.result()
        return JSONResponse(payload, status_code=status)

    async def _execute(
        self, spec: AgentSpec, function: AgentFunction, envelope: RunRequest, parsed: BaseModel
    ) -> tuple[int, dict[str, Any]]:
        steps = _CollectedSteps()
        context = RunContext(
            run_id=envelope.run_id,
            agent=spec.name,
            attempt=envelope.attempt,
            max_steps=spec.max_steps,
            content_policy=self._policy,
            proxy=None
            if envelope.proxy is None
            else ModelProxy(
                token=envelope.proxy.token,
                openai_base_url=envelope.proxy.openai_base_url,
                anthropic_base_url=envelope.proxy.anthropic_base_url,
            ),
        )
        expires = asyncio.get_running_loop().time() + envelope.deadline_ms / 1000
        approvals = (
            approval_scope(_CallbackApprovals(envelope.approvals_url, envelope.proxy.token))
            if envelope.approvals_url and envelope.proxy is not None
            else nullcontext()
        )
        try:
            with step_scope(steps), approvals, effect_faults_scope(envelope.effect_faults):
                async with deadline_scope(expires, RUN_DEADLINE):
                    result = await function(parsed, context)
            output = (
                result
                if isinstance(result, spec.output_model)
                else spec.output_model.model_validate(result)
            )
        except DeadlineExceeded:
            return 500, {"error": {"code": "AGENT_DEADLINE_EXCEEDED"}}
        except AgentError as error:
            return 500, {"error": {"code": error.code}}
        except ValidationError:
            return 500, {"error": {"code": "AGENT_OUTPUT_INVALID"}}
        except Exception:
            # The traceback stays in this process's own logs; RunWeave only sees the code.
            _logger.exception("Agent %s failed", spec.name)
            return 500, {"error": {"code": "RUN_FAILED"}}
        return 200, {"output": output.model_dump(mode="json"), "steps": steps.records()}

    async def _body(self, request: Request) -> bytes | None:
        declared = request.headers.get("content-length", "")
        if declared.isdecimal() and int(declared) > self._max_body:
            return None
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > self._max_body:
                return None
        return bytes(body)

    def _prune(self) -> None:
        now = asyncio.get_running_loop().time()
        for key in [k for k, entry in self._done.items() if entry[0] <= now]:
            del self._done[key]


async def _disconnect(request: Request) -> None:
    while (await request.receive())["type"] != "http.disconnect":
        pass


def create_agent_app(*functions: AgentFunction, **options: Any) -> Starlette:
    """An ASGI app serving the given agents. Options: see AgentServer."""
    return AgentServer(functions, **options).app


async def serve_agents(
    app: Any,
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    ready: Callable[[str], None] | None = None,
) -> None:
    """Serve on loopback and announce `RUNWEAVE_AGENT_READY <url>` once listening."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind((host, port))
    url = f"http://{host}:{sock.getsockname()[1]}"
    server = uvicorn.Server(
        uvicorn.Config(app, log_level="warning", lifespan="off", timeout_graceful_shutdown=5)
    )
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        while True:  # uvicorn exposes startup only as a flag
            if server.started or task.done():
                break
            await asyncio.sleep(0.01)
        if server.started:
            (ready or (lambda u: print(f"{READY_PREFIX} {u}", flush=True)))(url)
        await task
    finally:
        sock.close()
