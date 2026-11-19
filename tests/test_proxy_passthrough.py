"""Passthrough to a provider, tested only against scripted upstreams (never a real API)."""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import pytest
from proxy_harness import CHAT, MESSAGES, proxy_session
from pydantic import SecretStr, ValidationError

from runweave.core.settings import Settings
from runweave.limits import ProviderLimiter

OPENAI_KEY = "sk-upstream-openai-0123456789"
ANTHROPIC_KEY = "sk-ant-upstream-0123456789"
PASSTHROUGH: dict[str, Any] = {
    "proxy_mode": "passthrough",
    "proxy_openai_base_url": "https://openai.upstream.test/v1",
    "proxy_openai_api_key": SecretStr(OPENAI_KEY),
    "proxy_anthropic_base_url": "https://anthropic.upstream.test",
    "proxy_anthropic_api_key": SecretStr(ANTHROPIC_KEY),
    "proxy_models": ("gpt-test", "claude-test"),
    "model_input_usd_per_million": "1",
    "model_output_usd_per_million": "2",
    "model_price_version": "test-prices-1",
}
Handler = Callable[[httpx.Request], Awaitable[httpx.Response]]


class Upstream:
    def __init__(self, handler: Handler | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self.in_flight = 0
        self.peak = 0
        self._handler = handler

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            if self._handler:
                return await self._handler(request)
            if request.url.path.endswith("/messages"):
                return httpx.Response(
                    200,
                    json={
                        "id": "msg_up",
                        "type": "message",
                        "role": "assistant",
                        "model": "claude-test",
                        "content": [{"type": "text", "text": "upstream says hi"}],
                        "stop_reason": "end_turn",
                        "stop_sequence": None,
                        "usage": {"input_tokens": 11, "output_tokens": 5},
                    },
                )
            return httpx.Response(
                200,
                json={
                    "id": "chatcmpl-up",
                    "object": "chat.completion",
                    "created": 1,
                    "model": "gpt-test",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "upstream says hi"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 12, "completion_tokens": 6, "total_tokens": 18},
                },
            )
        finally:
            self.in_flight -= 1

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)


@pytest.mark.parametrize(
    "overrides",
    [
        {"proxy_openai_api_key": SecretStr(""), "proxy_anthropic_api_key": SecretStr("")},
        {"proxy_models": ()},
        {"model_input_usd_per_million": None},
        {"proxy_openai_base_url": "http://openai.upstream.test/v1"},
    ],
)
def test_passthrough_requires_keys_models_prices_and_https(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        Settings(**{**PASSTHROUGH, **overrides})


@pytest.mark.asyncio
async def test_chat_is_forwarded_with_the_provider_key_and_a_capped_limit(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    upstream = Upstream()
    async with proxy_session(upstream=upstream.transport(), **PASSTHROUGH) as s:
        response = await s.client.post(
            "/llm/v1/chat/completions", json={**CHAT, "max_tokens": 999_999}, headers=s.openai()
        )
        state = await s.ledger.read()
    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == "upstream says hi"
    sent = upstream.requests[0]
    assert str(sent.url) == "https://openai.upstream.test/v1/chat/completions"
    assert sent.headers["authorization"] == f"Bearer {OPENAI_KEY}"
    assert s.token not in sent.headers.get("authorization", "") + str(dict(sent.headers))
    body = json.loads(sent.content)
    assert body["model"] == "gpt-test"
    assert (
        body.get("max_completion_tokens", body.get("max_tokens")) <= state.policy.max_output_tokens
    )
    assert (state.input_tokens, state.output_tokens) == (12, 6)
    assert state.summary().estimated_cost_usd > 0
    detail = s.steps.details()[0][1] or ""
    assert detail.startswith("chat.completions · passthrough") and "12 in / 6 out" in detail
    assert OPENAI_KEY not in caplog.text and OPENAI_KEY not in response.text


@pytest.mark.asyncio
async def test_models_outside_the_allowlist_are_never_forwarded() -> None:
    upstream = Upstream()
    async with proxy_session(upstream=upstream.transport(), **PASSTHROUGH) as s:
        response = await s.client.post(
            "/llm/v1/chat/completions", json={**CHAT, "model": "gpt-expensive"}, headers=s.openai()
        )
    assert response.status_code == 400 and response.json()["error"]["code"] == "MODEL_NOT_ALLOWED"
    assert upstream.requests == []


@pytest.mark.asyncio
async def test_budgets_stop_overspend_before_forwarding() -> None:
    upstream = Upstream()
    async with proxy_session(
        upstream=upstream.transport(), agent_request_limit=1, **PASSTHROUGH
    ) as s:
        first = await s.client.post("/llm/v1/chat/completions", json=CHAT, headers=s.openai())
        second = await s.client.post("/llm/v1/chat/completions", json=CHAT, headers=s.openai())
    assert (first.status_code, second.status_code) == (200, 402)
    assert len(upstream.requests) == 1


@pytest.mark.asyncio
async def test_streams_relay_chunks_and_settle_streamed_usage() -> None:
    async def stream(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["stream_options"] == {"include_usage": True}
        chunks = [
            {
                "id": "c",
                "object": "chat.completion.chunk",
                "choices": [{"index": 0, "delta": {"content": "up"}}],
            },
            {
                "id": "c",
                "object": "chat.completion.chunk",
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            },
            {
                "id": "c",
                "object": "chat.completion.chunk",
                "choices": [],
                "usage": {"prompt_tokens": 20, "completion_tokens": 3, "total_tokens": 23},
            },
        ]
        text = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
        return httpx.Response(
            200, headers={"Content-Type": "text/event-stream"}, content=text.encode()
        )

    upstream = Upstream(stream)
    async with proxy_session(upstream=upstream.transport(), **PASSTHROUGH) as s:
        response = await s.client.post(
            "/llm/v1/chat/completions", json={**CHAT, "stream": True}, headers=s.openai()
        )
        state = await s.ledger.read()
    assert response.headers["content-type"].startswith("text/event-stream")
    assert '"content":"up"' in response.text.replace(" ", "") and "[DONE]" in response.text
    # The client did not ask for usage, so the usage-only chunk RunWeave requested is removed.
    assert '"usage"' not in response.text
    assert (state.input_tokens, state.output_tokens) == (20, 3)
    assert state.summary().uncertain_requests == 0


@pytest.mark.asyncio
async def test_anthropic_streams_settle_usage_from_message_events() -> None:
    async def stream(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-api-key"] == ANTHROPIC_KEY
        assert request.headers["anthropic-version"] == "2023-06-01"
        events = [
            (
                "message_start",
                {
                    "type": "message_start",
                    "message": {"id": "m", "usage": {"input_tokens": 9, "output_tokens": 1}},
                },
            ),
            (
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": "hi"},
                },
            ),
            (
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "end_turn"},
                    "usage": {"output_tokens": 4},
                },
            ),
            ("message_stop", {"type": "message_stop"}),
        ]
        text = "".join(f"event: {e}\ndata: {json.dumps(d)}\n\n" for e, d in events)
        return httpx.Response(
            200, headers={"Content-Type": "text/event-stream"}, content=text.encode()
        )

    upstream = Upstream(stream)
    async with proxy_session(upstream=upstream.transport(), **PASSTHROUGH) as s:
        response = await s.client.post(
            "/llm/v1/messages", json={**MESSAGES, "stream": True}, headers=s.anthropic()
        )
        state = await s.ledger.read()
    assert "message_stop" in response.text
    assert str(upstream.requests[0].url) == "https://anthropic.upstream.test/v1/messages"
    assert (state.input_tokens, state.output_tokens) == (9, 4)


@pytest.mark.asyncio
async def test_responses_api_usage_is_settled() -> None:
    async def respond(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["max_output_tokens"] >= 1
        return httpx.Response(
            200,
            json={
                "id": "resp",
                "object": "response",
                "status": "completed",
                "output": [],
                "usage": {"input_tokens": 7, "output_tokens": 2},
            },
        )

    upstream = Upstream(respond)
    async with proxy_session(upstream=upstream.transport(), **PASSTHROUGH) as s:
        response = await s.client.post(
            "/llm/v1/responses", json={"model": "gpt-test", "input": "hi"}, headers=s.openai()
        )
        state = await s.ledger.read()
    assert response.status_code == 200 and (state.input_tokens, state.output_tokens) == (7, 2)


@pytest.mark.asyncio
async def test_missing_usage_is_charged_conservatively() -> None:
    async def no_usage(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "c", "object": "chat.completion", "choices": []})

    async with proxy_session(upstream=httpx.MockTransport(Upstream(no_usage)), **PASSTHROUGH) as s:
        await s.client.post("/llm/v1/chat/completions", json=CHAT, headers=s.openai())
        state = await s.ledger.read()
    assert state.summary().uncertain_requests == 1
    assert state.output_tokens > 0  # The full reservation stays charged.


@pytest.mark.asyncio
async def test_upstream_errors_pass_through_without_retries_or_charges() -> None:
    async def limited(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            headers={"Retry-After": "3", "Set-Cookie": "x=1"},
            json={
                "error": {"message": "slow down", "type": "requests", "code": "rate_limit_exceeded"}
            },
        )

    upstream = Upstream(limited)
    async with proxy_session(upstream=upstream.transport(), **PASSTHROUGH) as s:
        response = await s.client.post("/llm/v1/chat/completions", json=CHAT, headers=s.openai())
        state = await s.ledger.read()
    assert response.status_code == 429 and response.headers["retry-after"] == "3"
    assert "set-cookie" not in response.headers
    assert response.json()["error"]["code"] == "rate_limit_exceeded"
    assert len(upstream.requests) == 1
    assert (state.input_tokens, state.output_tokens) == (0, 0)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "code"),
    [
        ("connect", "UPSTREAM_UNAVAILABLE"),
        ("redirect", "UPSTREAM_REDIRECT_DENIED"),
        ("huge", "UPSTREAM_RESPONSE_TOO_LARGE"),
    ],
)
async def test_upstream_transport_failures_are_classified(failure: str, code: str) -> None:
    async def broken(request: httpx.Request) -> httpx.Response:
        if failure == "connect":
            raise httpx.ConnectError("refused", request=request)
        if failure == "redirect":
            return httpx.Response(307, headers={"Location": "http://169.254.169.254/"})
        return httpx.Response(200, json={"choices": [], "padding": "x" * 9_000_000})

    upstream = Upstream(broken)
    async with proxy_session(upstream=upstream.transport(), **PASSTHROUGH) as s:
        response = await s.client.post("/llm/v1/chat/completions", json=CHAT, headers=s.openai())
    assert response.status_code == 502 and response.json()["error"]["code"] == code
    assert all(r.url.host.endswith("upstream.test") for r in upstream.requests)


@pytest.mark.asyncio
async def test_a_provider_without_a_key_is_never_called() -> None:
    upstream = Upstream()
    settings = {**PASSTHROUGH, "proxy_anthropic_api_key": SecretStr("")}
    async with proxy_session(upstream=upstream.transport(), **settings) as s:
        response = await s.client.post("/llm/v1/messages", json=MESSAGES, headers=s.anthropic())
    assert response.status_code == 400
    assert "not configured" in response.json()["error"]["message"]
    assert upstream.requests == []


@pytest.mark.asyncio
async def test_faults_apply_before_forwarding_in_passthrough() -> None:
    upstream = Upstream()
    settings = {**PASSTHROUGH, "proxy_fault_profile": "outage"}
    async with proxy_session(upstream=upstream.transport(), **settings) as s:
        response = await s.client.post("/llm/v1/chat/completions", json=CHAT, headers=s.openai())
    assert response.status_code == 503 and upstream.requests == []


@pytest.mark.asyncio
async def test_provider_concurrency_is_bounded_per_provider() -> None:
    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.1)
        return await Upstream()(request)

    upstream = Upstream(slow)
    settings = {**PASSTHROUGH, "proxy_openai_concurrency": 1, "proxy_anthropic_concurrency": 1}
    async with proxy_session(upstream=upstream.transport(), **settings) as s:
        responses = await asyncio.gather(
            # Small limits so all four reservations fit the run's output budget at once.
            *(
                s.client.post(
                    "/llm/v1/chat/completions", json={**CHAT, "max_tokens": 50}, headers=s.openai()
                )
                for _ in range(3)
            ),
            s.client.post(
                "/llm/v1/messages", json={**MESSAGES, "max_tokens": 50}, headers=s.anthropic()
            ),
        )
        assert all(r.status_code == 200 for r in responses)
    # One OpenAI call at a time, but the Anthropic call ran alongside it.
    assert upstream.peak == 2


@pytest.mark.asyncio
async def test_limiter_spaces_calls_and_respects_the_deadline() -> None:
    limiter = ProviderLimiter(concurrency=2, requests_per_minute=600)
    loop = asyncio.get_running_loop()
    started = loop.time()
    for _ in range(3):
        async with limiter.slot():
            pass
    # Two 0.1 s gaps, less Windows timer granularity (about 15.6 ms per tick).
    assert loop.time() - started >= 0.2 - 0.03
    slow = ProviderLimiter(concurrency=1, requests_per_minute=1)
    async with slow.slot():
        pass
    from runweave.deadline import RUN_DEADLINE, DeadlineExceeded, deadline_scope

    with pytest.raises(DeadlineExceeded):
        async with deadline_scope(loop.time() + 0.2, RUN_DEADLINE):
            async with slow.slot():
                pass
