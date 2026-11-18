"""RunWeave's side of the HTTP agent contract, against scripted transports."""

import asyncio
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest

from runweave.agents.http import (
    AgentUrlNotAllowed,
    HttpAgentConnector,
    ManifestRejected,
    RetryPolicy,
    SchemaViolation,
    normalize_agent_url,
)
from runweave.deadline import RUN_DEADLINE, DeadlineExceeded, deadline_scope
from runweave.http_contract import CONTRACT_VERSION, openapi_document
from runweave.sdk import AgentError, RunContext
from runweave.steps import StepEvent, step_scope

BASE = "http://127.0.0.1:9100"
INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "text": {"type": "string", "minLength": 1, "maxLength": 50},
        "count": {"type": "integer", "minimum": 0},
    },
    "required": ["text"],
    "additionalProperties": False,
}
OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {"reply": {"type": "string"}},
    "required": ["reply"],
}
Handler = Callable[[httpx.Request], Awaitable[httpx.Response]]


def manifest(**overrides: Any) -> dict[str, Any]:
    entry = {
        "name": "remote-echo",
        "version": "2.0.0",
        "description": "Echo over HTTP",
        "input_schema": INPUT_SCHEMA,
        "output_schema": OUTPUT_SCHEMA,
        "max_steps": 5,
    }
    entry.update(overrides)
    return {"contract": CONTRACT_VERSION, "agents": [entry]}


class Script:
    """Scripted agent server: manifest plus a queue of run responses."""

    def __init__(self, *runs: httpx.Response | Handler, manifest_body: Any = None) -> None:
        self.runs = list(runs)
        self.manifest_body = manifest() if manifest_body is None else manifest_body
        self.requests: list[httpx.Request] = []
        self.cancelled: list[str] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if request.method == "GET" and path == "/runweave/v1/agents":
            if isinstance(self.manifest_body, httpx.Response):
                return self.manifest_body
            return httpx.Response(200, json=self.manifest_body)
        if path.endswith("/cancel"):
            self.cancelled.append(path.split("/")[-2])
            return httpx.Response(202, json={})
        item = self.runs.pop(0)
        return await item(request) if callable(item) else item

    @property
    def run_requests(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == "POST" and r.url.path.endswith("/runs")]


def ok(output: dict[str, Any], steps: list[dict[str, Any]] | None = None) -> httpx.Response:
    return httpx.Response(200, json={"output": output, "steps": steps or []})


def connector(script: Script, **policy: Any) -> HttpAgentConnector:
    client = httpx.AsyncClient(transport=httpx.MockTransport(script), follow_redirects=False)
    return HttpAgentConnector(
        client,
        allowed_hosts=policy.pop("allowed_hosts", ()),
        retry=RetryPolicy(attempts=3, base_delay=0.01, max_delay=0.05),
        max_response_bytes=policy.pop("max_response_bytes", 4096),
        connect_timeout=1.0,
    )


class Steps:
    def __init__(self) -> None:
        self.events: dict[int, StepEvent] = {}

    async def begin(self, title: str, detail: str | None) -> StepEvent | None:
        from datetime import UTC, datetime

        event = StepEvent(
            index=len(self.events),
            title=title,
            detail=detail,
            status="running",
            started_at=datetime.now(UTC),
        )
        self.events[event.index] = event
        return event

    async def end(self, event: StepEvent) -> None:
        self.events[event.index] = event


def context(run_id: UUID | None = None, attempt: int = 1) -> RunContext:
    return RunContext(run_id=run_id or uuid4(), agent="remote-echo", attempt=attempt, max_steps=5)


async def run(script: Script, payload: dict[str, Any], **policy: Any) -> dict[str, Any]:
    agents = await connector(script, **policy).connect(BASE, removable=False)
    output = await agents[0].run(agents[0].parse(payload), context())
    return output.model_dump(mode="json")


async def failure(script: Script, payload: dict[str, Any] | None = None, **policy: Any) -> str:
    with pytest.raises(AgentError) as caught:
        await run(script, payload or {"text": "hi"}, **policy)
    return caught.value.code


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://127.0.0.1:9100", "http://127.0.0.1:9100"),
        ("http://127.0.0.1:9100/", "http://127.0.0.1:9100"),
        ("http://localhost:9100/agents/", "http://localhost:9100/agents"),
        ("http://[::1]:9100", "http://[::1]:9100"),
        ("https://127.0.0.2", "https://127.0.0.2"),
    ],
)
def test_loopback_urls_are_accepted_and_normalized(url: str, expected: str) -> None:
    assert normalize_agent_url(url, allowed_hosts=()) == expected


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com:9100",
        "http://10.0.0.5:9100",
        "http://169.254.169.254/latest",
        "http://127.0.0.1.nip.io:9100",
        "ftp://127.0.0.1:9100",
        "file:///etc/passwd",
        "http://user:secret@127.0.0.1:9100",
        "http://127.0.0.1:9100/?next=x",
        "http://127.0.0.1:9100/#frag",
        "127.0.0.1:9100",
        "http://127.0.0.1:99999",
        "http://0x7f000001:9100",
    ],
)
def test_non_loopback_and_unsafe_urls_are_rejected(url: str) -> None:
    with pytest.raises(AgentUrlNotAllowed):
        normalize_agent_url(url, allowed_hosts=())


def test_explicitly_allowed_hosts_are_the_only_non_loopback_destinations() -> None:
    allowed = ("agents.internal.test",)
    assert normalize_agent_url("https://Agents.Internal.test:8443", allowed_hosts=allowed) == (
        "https://agents.internal.test:8443"
    )
    with pytest.raises(AgentUrlNotAllowed):
        normalize_agent_url("https://other.internal.test", allowed_hosts=allowed)


@pytest.mark.asyncio
async def test_manifest_describes_agents_with_their_schemas() -> None:
    script = Script()
    agents = await connector(script).connect(BASE + "/", removable=True)
    info = agents[0].info
    assert (info.name, info.version, info.kind, info.target) == (
        "remote-echo",
        "2.0.0",
        "http",
        BASE,
    )
    assert info.input_schema == INPUT_SCHEMA and info.output_schema == OUTPUT_SCHEMA
    assert info.max_steps == 5 and info.removable is True
    assert script.requests[0].headers["accept"] == "application/json"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "code"),
    [
        (
            {"contract": "runweave.agent/v9", "agents": manifest()["agents"]},
            "AGENT_MANIFEST_INVALID",
        ),
        ({"contract": CONTRACT_VERSION, "agents": []}, "AGENT_MANIFEST_INVALID"),
        (manifest(name="Bad Name"), "AGENT_MANIFEST_INVALID"),
        (manifest(input_schema={"type": "array"}), "AGENT_MANIFEST_INVALID"),
        (manifest(input_schema={"type": "object", "minProperties": "x"}), "AGENT_MANIFEST_INVALID"),
        (manifest(name="incident-triage"), "AGENT_MANIFEST_INVALID"),
        (httpx.Response(200, text="<html>"), "AGENT_MANIFEST_INVALID"),
        (
            httpx.Response(200, json={"contract": CONTRACT_VERSION, "agents": "x" * 9000}),
            "AGENT_MANIFEST_INVALID",
        ),
        (httpx.Response(404, json={}), "AGENT_MANIFEST_INVALID"),
        (
            httpx.Response(302, headers={"Location": "http://169.254.169.254/"}),
            "AGENT_REDIRECT_DENIED",
        ),
    ],
)
async def test_invalid_manifests_are_rejected_with_a_code(body: Any, code: str) -> None:
    with pytest.raises(ManifestRejected) as caught:
        await connector(Script(manifest_body=body)).connect(BASE, removable=False)
    assert caught.value.code == code


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "schema",
    [
        {"type": "object", "properties": {"x": {"$ref": "https://schemas.example/x.json"}}},
        {"type": "object", "$defs": {"a": {"$ref": "other.json#/a"}}},
        {"type": "object", "properties": {"x": {"$dynamicRef": "http://127.0.0.1/s"}}},
    ],
)
async def test_schemas_may_only_reference_themselves(schema: dict[str, Any]) -> None:
    with pytest.raises(ManifestRejected) as caught:
        await connector(Script(manifest_body=manifest(input_schema=schema))).connect(
            BASE, removable=False
        )
    assert caught.value.code == "AGENT_MANIFEST_INVALID"


@pytest.mark.asyncio
async def test_local_references_from_pydantic_models_are_supported() -> None:
    schema = {
        "type": "object",
        "$defs": {"Inner": {"type": "object", "properties": {"n": {"type": "integer"}}}},
        "properties": {"inner": {"$ref": "#/$defs/Inner"}},
    }
    agents = await connector(Script(manifest_body=manifest(input_schema=schema))).connect(
        BASE, removable=False
    )
    with pytest.raises(SchemaViolation) as caught:
        agents[0].parse({"inner": {"n": "nope"}})
    assert caught.value.errors[0]["loc"] == ["inner", "n"]


@pytest.mark.asyncio
async def test_duplicate_names_in_one_manifest_are_rejected() -> None:
    body = manifest()
    body["agents"] = body["agents"] * 2
    with pytest.raises(ManifestRejected):
        await connector(Script(manifest_body=body)).connect(BASE, removable=False)


@pytest.mark.asyncio
async def test_unreachable_agents_report_a_code() -> None:
    async def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(refuse))
    target = HttpAgentConnector(
        client,
        allowed_hosts=(),
        retry=RetryPolicy(1, 0.01, 0.01),
        max_response_bytes=4096,
        connect_timeout=1.0,
    )
    with pytest.raises(ManifestRejected) as caught:
        await target.connect(BASE, removable=False)
    assert caught.value.code == "AGENT_UNREACHABLE"


@pytest.mark.asyncio
async def test_input_is_validated_against_the_schema_without_echoing_values() -> None:
    agents = await connector(Script()).connect(BASE, removable=False)
    with pytest.raises(SchemaViolation) as caught:
        agents[0].parse({"text": "<script>" * 10, "count": -4, "secret-key-name": 1})
    errors = caught.value.errors
    assert {tuple(e["loc"]) for e in errors} >= {("text",), ("count",)}
    assert all(set(e) == {"loc", "type", "msg"} for e in errors)
    assert "<script>" not in json.dumps(errors) and "secret-key-name" not in json.dumps(errors)
    with pytest.raises(SchemaViolation) as missing:
        agents[0].parse({})
    assert missing.value.errors[0]["loc"] == ["text"]
    assert agents[0].parse({"text": "ok"}).model_dump() == {"text": "ok"}


@pytest.mark.asyncio
async def test_successful_run_sends_the_contract_request_and_records_remote_steps() -> None:
    steps: list[dict[str, Any]] = [
        {"title": "Plan", "detail": "token sk-test", "status": "succeeded", "duration_ms": 12.5},
        {"title": "Answer", "detail": None, "status": "failed", "duration_ms": 3},
    ]
    script = Script(ok({"reply": "hello"}, steps))
    agents = await connector(script).connect(BASE, removable=False)
    run_id = uuid4()
    log = Steps()
    loop = asyncio.get_running_loop()
    with step_scope(log):
        async with deadline_scope(loop.time() + 10, RUN_DEADLINE):
            output = await agents[0].run(
                agents[0].parse({"text": "hi"}), context(run_id, attempt=2)
            )
    assert output.model_dump() == {"reply": "hello"}
    request = script.run_requests[0]
    assert request.url.path == "/runweave/v1/agents/remote-echo/runs"
    assert request.headers["idempotency-key"] == str(run_id)
    body = json.loads(request.content)
    assert body["run_id"] == str(run_id) and body["attempt"] == 2
    assert body["input"] == {"text": "hi"}
    assert 9000 < body["deadline_ms"] <= 10000
    recorded = list(log.events.values())
    assert [(e.title, e.status) for e in recorded] == [("Plan", "succeeded"), ("Answer", "failed")]
    assert recorded[0].duration_ms == pytest.approx(12.5)


@pytest.mark.asyncio
async def test_output_must_match_the_output_schema() -> None:
    assert await failure(Script(ok({"reply": 7}))) == "AGENT_OUTPUT_INVALID"


@pytest.mark.asyncio
async def test_more_steps_than_declared_fail_the_run() -> None:
    steps = [{"title": f"s{i}", "status": "succeeded", "duration_ms": 1} for i in range(6)]
    assert await failure(Script(ok({"reply": "x"}, steps))) == "AGENT_STEP_LIMIT"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, text="not json", headers={"Content-Type": "application/json"}),
        httpx.Response(200, json=["output"]),
        httpx.Response(200, json={"steps": []}),
        httpx.Response(200, json={"output": "text"}),
        httpx.Response(200, text='{"output": {}}', headers={"Content-Type": "text/html"}),
        httpx.Response(204),
    ],
)
async def test_malformed_responses_are_classified(response: httpx.Response) -> None:
    assert await failure(Script(response)) == "AGENT_RESPONSE_INVALID"


@pytest.mark.asyncio
async def test_oversized_responses_are_rejected_by_declared_and_streamed_size() -> None:
    big = ok({"reply": "x" * 10000})
    assert await failure(Script(big), max_response_bytes=4096) == "AGENT_RESPONSE_TOO_LARGE"

    async def chunks() -> Any:
        for _ in range(20):
            yield b"x" * 1024

    streamed = httpx.Response(200, headers={"Content-Type": "application/json"}, content=chunks())
    assert await failure(Script(streamed), max_response_bytes=4096) == "AGENT_RESPONSE_TOO_LARGE"


@pytest.mark.asyncio
async def test_redirects_are_denied_and_never_followed() -> None:
    script = Script(httpx.Response(307, headers={"Location": "http://169.254.169.254/"}))
    assert await failure(script) == "AGENT_REDIRECT_DENIED"
    assert all(r.url.host == "127.0.0.1" for r in script.requests)


@pytest.mark.asyncio
async def test_transient_statuses_are_retried_with_the_same_idempotency_key() -> None:
    script = Script(
        httpx.Response(503, headers={"Retry-After": "0"}),
        httpx.Response(429),
        ok({"reply": "third time"}),
    )
    assert await run(script, {"text": "hi"}) == {"reply": "third time"}
    keys = {r.headers["idempotency-key"] for r in script.run_requests}
    assert len(script.run_requests) == 3 and len(keys) == 1


@pytest.mark.asyncio
async def test_retries_are_bounded_and_end_as_unavailable() -> None:
    script = Script(*(httpx.Response(502) for _ in range(5)))
    assert await failure(script) == "AGENT_UNAVAILABLE"
    assert len(script.run_requests) == 3


@pytest.mark.asyncio
async def test_retry_after_beyond_the_deadline_is_not_waited_for() -> None:
    script = Script(httpx.Response(503, headers={"Retry-After": "60"}), ok({"reply": "late"}))
    agents = await connector(script).connect(BASE, removable=False)
    loop = asyncio.get_running_loop()
    with pytest.raises(AgentError) as caught:
        async with deadline_scope(loop.time() + 5, RUN_DEADLINE):
            await agents[0].run(agents[0].parse({"text": "hi"}), context())
    assert caught.value.code == "AGENT_UNAVAILABLE"
    assert len(script.run_requests) == 1


@pytest.mark.asyncio
async def test_connection_failures_are_retried_but_lost_responses_are_not() -> None:
    async def refused(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    async def dropped(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadError("connection reset", request=request)

    assert await run(Script(refused, ok({"reply": "up"})), {"text": "hi"}) == {"reply": "up"}
    script = Script(dropped, ok({"reply": "never"}))
    assert await failure(script) == "AGENT_CONNECTION_LOST"
    assert len(script.run_requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "code"),
    [
        (httpx.Response(500, text="Traceback: /home/dev/secret.py"), "AGENT_SERVER_ERROR"),
        (httpx.Response(501), "AGENT_SERVER_ERROR"),
        (httpx.Response(500, json={"error": {"code": "RUN_FAILED"}}), "RUN_FAILED"),
        (httpx.Response(500, json={"error": {"code": "AGENT_STEP_LIMIT"}}), "AGENT_STEP_LIMIT"),
        (
            httpx.Response(500, json={"error": {"code": "AGENT_OUTPUT_INVALID"}}),
            "AGENT_OUTPUT_INVALID",
        ),
        (
            httpx.Response(500, json={"error": {"code": "ANYTHING_THE_AGENT_SAYS"}}),
            "AGENT_SERVER_ERROR",
        ),
        (httpx.Response(422, json={"error": {"code": "INVALID_INPUT"}}), "AGENT_INPUT_REJECTED"),
        (httpx.Response(404), "AGENT_REQUEST_REJECTED"),
        (httpx.Response(400), "AGENT_REQUEST_REJECTED"),
    ],
)
async def test_error_statuses_are_classified_without_retrying(
    response: httpx.Response, code: str
) -> None:
    script = Script(response, ok({"reply": "never"}))
    assert await failure(script) == code
    assert len(script.run_requests) == 1


@pytest.mark.asyncio
async def test_agent_reported_deadline_becomes_a_run_timeout() -> None:
    script = Script(httpx.Response(500, json={"error": {"code": "AGENT_DEADLINE_EXCEEDED"}}))
    agents = await connector(script).connect(BASE, removable=False)
    with pytest.raises(DeadlineExceeded):
        await agents[0].run(agents[0].parse({"text": "hi"}), context())


@pytest.mark.asyncio
async def test_deadline_closes_the_request_and_sends_a_cancel_call() -> None:
    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(30)
        return ok({"reply": "late"})

    script = Script(slow)
    agents = await connector(script).connect(BASE, removable=False)
    run_id = uuid4()
    loop = asyncio.get_running_loop()
    started = loop.time()
    with pytest.raises(DeadlineExceeded):
        async with deadline_scope(loop.time() + 0.2, RUN_DEADLINE):
            await agents[0].run(agents[0].parse({"text": "hi"}), context(run_id))
    assert loop.time() - started < 2
    assert script.cancelled == [str(run_id)]


@pytest.mark.asyncio
async def test_cancellation_sends_a_best_effort_cancel_call_and_propagates() -> None:
    entered = asyncio.Event()

    async def slow(request: httpx.Request) -> httpx.Response:
        entered.set()
        await asyncio.sleep(30)
        return ok({"reply": "late"})

    script = Script(slow)
    agents = await connector(script).connect(BASE, removable=False)
    run_id = uuid4()
    task = asyncio.create_task(agents[0].run(agents[0].parse({"text": "hi"}), context(run_id)))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert script.cancelled == [str(run_id)]


@pytest.mark.asyncio
async def test_a_failing_cancel_call_does_not_mask_cancellation() -> None:
    entered = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=manifest())
        if request.url.path.endswith("/cancel"):
            raise httpx.ConnectError("gone", request=request)
        entered.set()
        await asyncio.sleep(30)
        return ok({"reply": "late"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    target = HttpAgentConnector(
        client,
        allowed_hosts=(),
        retry=RetryPolicy(1, 0.01, 0.01),
        max_response_bytes=4096,
        connect_timeout=1.0,
    )
    agents = await target.connect(BASE, removable=False)
    task = asyncio.create_task(agents[0].run(agents[0].parse({"text": "hi"}), context()))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_committed_openapi_contract_matches_the_models() -> None:
    path = Path(__file__).parents[1] / "contracts" / "agent-http-v1.openapi.json"
    committed = json.loads(path.read_text(encoding="utf-8"))
    # Regenerate with: python -m runweave.http_contract
    assert committed == openapi_document()
    assert committed["info"]["version"] == CONTRACT_VERSION
    assert set(committed["paths"]) == {
        "/runweave/v1/agents",
        "/runweave/v1/agents/{name}/runs",
        "/runweave/v1/agents/{name}/runs/{run_id}/cancel",
    }
