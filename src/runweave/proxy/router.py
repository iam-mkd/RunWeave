"""Local LLM proxy: OpenAI Chat Completions and Responses at /llm/v1, Anthropic Messages at
/llm/v1/messages (Anthropic base URL /llm). Every request needs a live run's proxy token.

Per call, in order: authenticate the token, parse the request, check the passthrough model
allowlist, pick a seeded fault (error faults answer here and cost nothing), reserve budget,
then answer from the mock or the provider. Usage is settled afterwards (a stream cut short
keeps its full reservation) and each call becomes a "Model call" timeline step. Prompts
and completions are never logged or stored.
"""

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Iterator
from contextlib import AsyncExitStack, suppress
from typing import Any, cast

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from runweave.budgets import BudgetExceeded, Reservation
from runweave.core.runtime import Runtime
from runweave.proxy.bindings import Binding
from runweave.proxy.config import ProxyConfig
from runweave.proxy.faults import ERROR_FAULTS, FaultSpec
from runweave.proxy.mock import Api, ModelReply, ModelRequest, broken_tool_call
from runweave.proxy.upstream import Upstream, UpstreamFailure, Usage, usage_from_body
from runweave.proxy.wire import RENDER, STREAM, WireError, ids_for, parse
from runweave.steps import StepEvent, StepStatus, finished_event

router = APIRouter(prefix="/llm/v1", tags=["llm proxy"])
LABELS: dict[Api, str] = {
    "chat": "chat.completions",
    "responses": "responses",
    "messages": "messages",
}
_OPENAI_TYPES = {
    400: "invalid_request_error",
    401: "invalid_request_error",
    402: "insufficient_quota",
    429: "requests",
    502: "server_error",
    503: "server_error",
    504: "server_error",
}
_ANTHROPIC_TYPES = {
    400: "invalid_request_error",
    401: "authentication_error",
    402: "billing_error",
    429: "rate_limit_error",
    500: "api_error",
    502: "api_error",
    503: "overloaded_error",
    504: "api_error",
}


class StreamCut(RuntimeError):
    """Raised inside a stream to drop the connection mid-response (stream_disconnect fault)."""


class _QuietStreamCuts(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not (record.exc_info and isinstance(record.exc_info[1], StreamCut))


def install_log_filter() -> None:
    """Injected disconnects are expected; keep them out of the server's error log."""
    logger = logging.getLogger("uvicorn.error")
    if not any(isinstance(f, _QuietStreamCuts) for f in logger.filters):
        logger.addFilter(_QuietStreamCuts())


def _error(
    api: Api, status: int, code: str, message: str, headers: dict[str, str] | None = None
) -> JSONResponse:
    if api == "messages":
        body: dict[str, Any] = {
            "type": "error",
            "error": {"type": _ANTHROPIC_TYPES.get(status, "api_error"), "message": message},
        }
    else:
        body = {
            "error": {
                "message": message,
                "type": _OPENAI_TYPES.get(status, "server_error"),
                "param": None,
                "code": code,
            }
        }
    return JSONResponse(
        body, status_code=status, headers={"Cache-Control": "no-store", **(headers or {})}
    )


def _token(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    if header[:7].lower() == "bearer ":
        return header[7:].strip()
    return request.headers.get("x-api-key")


class _Call:
    """One model call on the run timeline."""

    def __init__(self, binding: Binding, label: str, fault: str | None) -> None:
        self._binding = binding
        self._label = label
        self._fault = fault
        self._event: StepEvent | None = None

    async def begin(self) -> None:
        if self._binding.steps is not None:
            with suppress(Exception):
                self._event = await self._binding.steps.begin("Model call", self._label)

    async def end(self, status: StepStatus, outcome: str) -> None:
        if self._binding.steps is not None and self._event is not None:
            with suppress(Exception):
                done = finished_event(self._event, status)
                await self._binding.steps.end(
                    done.model_copy(
                        update={
                            "detail": f"{self._label} · {outcome}"[:1000],
                            "kind": "model_call",
                            "fault": self._fault,
                        }
                    )
                )


async def _error_fault(api: Api, fault: FaultSpec, binding: Binding) -> JSONResponse:
    message = "RunWeave injected fault"
    if fault.kind == "rate_limit":
        return _error(
            api,
            429,
            "rate_limit_exceeded",
            f"Rate limit reached ({message})",
            {"Retry-After": str(fault.retry_after)},
        )
    if fault.kind == "server_error":
        return _error(api, fault.status, "server_error", f"The server had an error ({message})")
    if fault.kind == "context_length":
        return _error(
            api,
            400,
            "context_length_exceeded",
            f"prompt is too long: the context length was exceeded ({message})",
        )
    hold = max(0.0, min(fault.seconds, binding.expires_at - asyncio.get_running_loop().time()))
    await asyncio.sleep(hold)
    return _error(api, 504, "timeout", f"The request timed out ({message})")


def _reply_events(
    api: Api, request: ModelRequest, reply: ModelReply, binding: Binding
) -> Iterator[bytes]:
    return STREAM[api](request, reply, ids_for(reply, binding.call_id, api))


async def _proxy(api: Api, request: Request) -> Response:
    runtime = cast(Runtime | None, getattr(request.app.state, "runtime", None))
    if runtime is None or runtime.proxy is None:
        return _error(api, 503, "PROXY_UNAVAILABLE", "RunWeave is not ready")
    binding = runtime.proxy.resolve(_token(request))
    if binding is None:
        return _error(
            api,
            401,
            "PROXY_TOKEN_INVALID",
            "Use the proxy token of a running RunWeave run as the API key",
        )
    config = cast(ProxyConfig, request.app.state.proxy_config)
    raw = await request.body()
    try:
        body = json.loads(raw)
        model_request = parse(api, body, len(raw))
    except (ValueError, WireError) as error:
        message = str(error) if isinstance(error, WireError) else "The body is not valid JSON"
        return _error(api, 400, "INVALID_REQUEST", message)
    passthrough = config.mode == "passthrough"
    if passthrough and model_request.model not in config.models:
        return _error(
            api,
            400,
            "MODEL_NOT_ALLOWED",
            "This model is not in RUNWEAVE_PROXY_MODELS; it would not be priced",
        )
    if passthrough and not cast(Upstream, runtime.upstream).configured(api):
        return _error(
            api,
            400,
            "PROVIDER_NOT_CONFIGURED",
            "This provider's API key is not configured in RunWeave",
        )

    fault = config.faults.choose(next(binding.calls), model_request)
    label = f"{LABELS[api]} · {config.mode}" + (" · stream" if model_request.stream else "")
    if fault is not None and fault.kind not in ERROR_FAULTS:
        label += f" · fault: {fault.kind}"
    call = _Call(binding, label, fault.kind if fault is not None else None)
    await call.begin()
    if fault is not None and fault.kind in ERROR_FAULTS:
        response = await _error_fault(api, fault, binding)
        await call.end("failed", f"fault: {fault.kind} ({response.status_code})")
        return response

    policy = (await binding.ledger.read()).policy
    requested = model_request.max_tokens or config.default_max_tokens
    try:
        # One token per request byte plus framing slack, as for the built-in agents.
        reservation = await Reservation.reserve(
            binding.ledger, policy.model, len(raw) + 1024, requested
        )
    except BudgetExceeded as error:
        await call.end("failed", f"blocked: {error.code}")
        return _error(api, 402, error.code, "The run's model budget does not allow this call")

    delay = fault.seconds if fault is not None and fault.kind == "slow_first_token" else 0.0
    cut_after = (
        fault.after_chunks if fault is not None and fault.kind == "stream_disconnect" else None
    )
    tool_fault = fault is not None and fault.kind in {"malformed_tool_call", "unknown_tool"}
    if passthrough and not tool_fault:
        upstream = cast(Upstream, runtime.upstream)
        return await _forward(
            api,
            request,
            body,
            model_request,
            binding,
            reservation,
            call,
            upstream,
            delay,
            cut_after,
        )

    if tool_fault:
        reply = broken_tool_call(
            model_request,
            unknown=fault is not None and fault.kind == "unknown_tool",
            output_limit=reservation.output_limit,
        )
    else:
        reply = config.mock.reply(model_request, output_limit=reservation.output_limit)
    usage = f"{reply.input_tokens} in / {reply.output_tokens} out"
    if not model_request.stream:
        await asyncio.sleep(delay)
        with suppress(BudgetExceeded):
            await reservation.settle(reply.input_tokens, reply.output_tokens)
        await call.end("succeeded", usage)
        return JSONResponse(RENDER[api](model_request, reply, ids_for(reply, binding.call_id, api)))

    async def mock_events() -> AsyncIterator[bytes]:
        await asyncio.sleep(delay)
        for chunk in _reply_events(api, model_request, reply, binding):
            yield chunk

    return _stream(
        mock_events(),
        reservation,
        call,
        cut_after,
        lambda: Usage(reply.input_tokens, reply.output_tokens),
    )


def _stream(
    source: AsyncIterator[bytes],
    reservation: Reservation,
    call: _Call,
    cut_after: int | None,
    usage: Any,
) -> StreamingResponse:
    async def events() -> AsyncIterator[bytes]:
        completed = False
        outcome = "stream closed early"
        try:
            sent = 0
            async for chunk in source:
                if cut_after is not None and sent >= cut_after:
                    outcome = "stream cut by fault"
                    raise StreamCut("Injected mid-stream disconnect")
                yield chunk
                sent += 1
            completed = True
        except UpstreamFailure as failure:
            outcome = failure.code
            raise StreamCut(failure.code) from None
        finally:
            found: Usage = usage()
            with suppress(Exception):
                if completed:
                    await reservation.settle(found.input_tokens, found.output_tokens)
                else:
                    await reservation.settle(None, None)
            if completed:
                outcome = f"{found.input_tokens} in / {found.output_tokens} out"
                if found.input_tokens is None or found.output_tokens is None:
                    outcome += " (usage missing; reservation charged)"
            await call.end("succeeded" if completed else "failed", outcome)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


async def _forward(
    api: Api,
    request: Request,
    body: dict[str, Any],
    model_request: ModelRequest,
    binding: Binding,
    reservation: Reservation,
    call: _Call,
    upstream: Upstream,
    delay: float,
    cut_after: int | None,
) -> Response:
    prepared = upstream.prepare(api, body, reservation.output_limit, request.headers)
    # The stack's contexts may be closed by the streaming task, so nothing in it may be
    # task-bound (no deadline scope); the deadline bounds the wait and the provider timeouts.
    stack = AsyncExitStack()
    try:
        async with asyncio.timeout_at(binding.expires_at):
            await stack.enter_async_context(upstream.limiter(api).slot())
            await asyncio.sleep(delay)
        response = await stack.enter_async_context(upstream.open(prepared, binding.expires_at))
        if not (200 <= response.status_code < 300) or not model_request.stream:
            relayed = await upstream.read(response)
            await stack.aclose()
    except (UpstreamFailure, TimeoutError) as failure:
        await stack.aclose()
        code = failure.code if isinstance(failure, UpstreamFailure) else "UPSTREAM_TIMEOUT"
        charged = isinstance(failure, UpstreamFailure) and failure.charged
        with suppress(Exception):
            await reservation.settle(None, None) if charged else await reservation.settle(0, 0)
        await call.end("failed", code)
        status = 504 if code == "UPSTREAM_TIMEOUT" else 502
        return _error(api, status, code, "The provider could not be reached or answered badly")
    except BaseException:
        await stack.aclose()
        raise

    if not (200 <= response.status_code < 300) or not model_request.stream:
        if 200 <= response.status_code < 300:
            try:
                found = usage_from_body(api, json.loads(relayed.content))
            except ValueError:
                found = Usage()
            with suppress(BudgetExceeded):
                await reservation.settle(found.input_tokens, found.output_tokens)
            outcome = f"{found.input_tokens} in / {found.output_tokens} out"
            if found.input_tokens is None or found.output_tokens is None:
                outcome += " (usage missing; reservation charged)"
            await call.end("succeeded", outcome)
        else:
            # Providers do not bill rejected requests; RunWeave does not retry them either.
            with suppress(BudgetExceeded):
                await reservation.settle(0, 0)
            await call.end("failed", f"provider answered {response.status_code}")
        return Response(
            relayed.content,
            status_code=relayed.status,
            headers={**relayed.headers, "Cache-Control": "no-store"},
        )

    usage = Usage()

    async def relay() -> AsyncIterator[bytes]:
        try:
            async for event in upstream.events(
                api, response, usage, strip_usage=prepared.strip_usage
            ):
                yield event
        finally:
            await stack.aclose()

    return _stream(relay(), reservation, call, cut_after, lambda: usage)


@router.post("/chat/completions", summary="OpenAI Chat Completions")
async def chat_completions(request: Request) -> Response:
    return await _proxy("chat", request)


@router.post("/responses", summary="OpenAI Responses")
async def responses(request: Request) -> Response:
    return await _proxy("responses", request)


@router.post("/messages", summary="Anthropic Messages")
async def messages(request: Request) -> Response:
    return await _proxy("messages", request)
