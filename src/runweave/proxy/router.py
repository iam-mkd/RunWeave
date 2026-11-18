"""Local LLM proxy: OpenAI Chat Completions and Responses at /llm/v1, Anthropic Messages at
/llm/v1/messages (Anthropic base URL /llm). Every request needs a live run's proxy token.

Each call reserves budget before it is answered, settles reported usage afterwards (a
stream cut short is charged its full reservation), and adds a "Model call" step to the run
timeline. Prompts and completions are never logged or stored.
"""

import json
from collections.abc import AsyncIterator
from contextlib import suppress
from typing import Any, cast

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from runweave.budgets import BudgetExceeded, Reservation
from runweave.core.runtime import Runtime
from runweave.proxy.bindings import Binding
from runweave.proxy.mock import Api, MockModel
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
    503: "server_error",
}
_ANTHROPIC_TYPES = {
    400: "invalid_request_error",
    401: "authentication_error",
    402: "billing_error",
    503: "overloaded_error",
}


def _error(api: Api, status: int, code: str, message: str) -> JSONResponse:
    if api == "messages":
        body: dict[str, Any] = {
            "type": "error",
            "error": {"type": _ANTHROPIC_TYPES[status], "message": message},
        }
    else:
        body = {
            "error": {
                "message": message,
                "type": _OPENAI_TYPES[status],
                "param": None,
                "code": code,
            }
        }
    return JSONResponse(body, status_code=status, headers={"Cache-Control": "no-store"})


def _token(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    if header[:7].lower() == "bearer ":
        return header[7:].strip()
    return request.headers.get("x-api-key")


class _Call:
    """One model call on the run timeline."""

    def __init__(self, binding: Binding, api: Api, stream: bool) -> None:
        self._binding = binding
        self._label = f"{LABELS[api]} · mock" + (" · stream" if stream else "")
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
                    done.model_copy(update={"detail": f"{self._label} · {outcome}"[:1000]})
                )


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
    raw = await request.body()
    try:
        model_request = parse(api, json.loads(raw), len(raw))
    except (ValueError, WireError) as error:
        message = str(error) if isinstance(error, WireError) else "The body is not valid JSON"
        return _error(api, 400, "INVALID_REQUEST", message)

    call = _Call(binding, api, model_request.stream)
    await call.begin()
    policy = (await binding.ledger.read()).policy
    requested = model_request.max_tokens or int(request.app.state.proxy_default_max_tokens)
    try:
        # One token per request byte plus framing slack, as for the built-in agents.
        reservation = await Reservation.reserve(
            binding.ledger, policy.model, len(raw) + 1024, requested
        )
    except BudgetExceeded as error:
        await call.end("failed", f"blocked: {error.code}")
        return _error(api, 402, error.code, "The run's model budget does not allow this call")

    mock = cast(MockModel, request.app.state.proxy_mock)
    reply = mock.reply(model_request, output_limit=reservation.output_limit)
    ids = ids_for(reply, binding.call_id, api)
    usage = f"{reply.input_tokens} in / {reply.output_tokens} out"
    if not model_request.stream:
        with suppress(BudgetExceeded):
            await reservation.settle(reply.input_tokens, reply.output_tokens)
        await call.end("succeeded", usage)
        return JSONResponse(RENDER[api](model_request, reply, ids))

    async def events() -> AsyncIterator[bytes]:
        completed = False
        try:
            for chunk in STREAM[api](model_request, reply, ids):
                yield chunk
            completed = True
        finally:
            # A stream cut short keeps its full reservation: usage is unknown.
            with suppress(Exception):
                if completed:
                    await reservation.settle(reply.input_tokens, reply.output_tokens)
                else:
                    await reservation.settle(None, None)
            await call.end(
                "succeeded" if completed else "failed",
                usage if completed else "stream closed early",
            )

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@router.post("/chat/completions", summary="OpenAI Chat Completions (mock mode)")
async def chat_completions(request: Request) -> Response:
    return await _proxy("chat", request)


@router.post("/responses", summary="OpenAI Responses (mock mode)")
async def responses(request: Request) -> Response:
    return await _proxy("responses", request)


@router.post("/messages", summary="Anthropic Messages (mock mode)")
async def messages(request: Request) -> Response:
    return await _proxy("messages", request)
