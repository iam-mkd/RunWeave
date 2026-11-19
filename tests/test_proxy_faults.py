"""Seeded fault profiles: the same seed reproduces the same faults."""

import json
import logging
from pathlib import Path
from typing import Any

import httpx
import pytest
from proxy_harness import CHAT, CHAT_TOOLS, MESSAGES, proxy_session
from pydantic import ValidationError

from runweave.proxy.faults import BUILT_IN_PROFILES, FaultPlan, FaultProfile, load_profiles
from runweave.proxy.mock import ModelRequest


def request(*, stream: bool = False, tools: tuple[str, ...] = ()) -> ModelRequest:
    return ModelRequest(
        api="chat", model="m", last_role="user", last_user_text="hi", tool_names=tools,
        stream=stream, include_usage=False, max_tokens=None, payload_bytes=100,
    )


def profile(*faults: dict[str, Any]) -> FaultProfile:
    return FaultProfile.model_validate({"name": "test", "faults": list(faults)})


def test_built_in_profiles_are_valid_and_named() -> None:
    assert {"flaky", "rate-limited", "outage", "slow", "broken-tools", "chaos"} <= set(BUILT_IN_PROFILES)
    assert all(sum(f.probability for f in p.faults) <= 1 for p in BUILT_IN_PROFILES.values())


@pytest.mark.parametrize(
    "faults",
    [
        [{"kind": "rate_limit", "probability": 0.7}, {"kind": "server_error", "probability": 0.5}],
        [{"kind": "explode", "probability": 0.1}],
        [{"kind": "server_error", "probability": 0.1, "status": 404}],
        [{"kind": "timeout", "probability": 0.1, "seconds": 10_000}],
        [{"kind": "rate_limit", "probability": -0.1}],
        [],
    ],
)
def test_invalid_profiles_are_rejected(faults: list[dict[str, Any]]) -> None:
    with pytest.raises(ValidationError):
        profile(*faults)


def test_the_same_seed_reproduces_the_same_faults() -> None:
    chaos = BUILT_IN_PROFILES["chaos"]
    first = [FaultPlan(chaos, seed=7).choose(i, request(stream=True, tools=("t",))) for i in range(200)]
    again = [FaultPlan(chaos, seed=7).choose(i, request(stream=True, tools=("t",))) for i in range(200)]
    other = [FaultPlan(chaos, seed=8).choose(i, request(stream=True, tools=("t",))) for i in range(200)]
    kinds = [f.kind if f else None for f in first]
    assert kinds == [f.kind if f else None for f in again]
    assert kinds != [f.kind if f else None for f in other]
    assert len({k for k in kinds if k}) >= 6


def test_probabilities_are_respected_over_many_calls() -> None:
    plan = FaultPlan(profile({"kind": "rate_limit", "probability": 0.25}), seed=1)
    hits = sum(plan.choose(i, request()) is not None for i in range(4000))
    assert 900 < hits < 1100


def test_faults_only_apply_where_they_make_sense() -> None:
    plan = FaultPlan(
        profile({"kind": "stream_disconnect", "probability": 0.5},
                {"kind": "unknown_tool", "probability": 0.5}),
        seed=3,
    )
    assert all(plan.choose(i, request()) is None for i in range(100))
    streamed = {f.kind for i in range(100) if (f := plan.choose(i, request(stream=True)))}
    assert streamed == {"stream_disconnect"}
    restricted = FaultPlan(profile({"kind": "rate_limit", "probability": 1, "apis": ["messages"]}), seed=1)
    assert restricted.choose(0, request()) is None


def test_profile_files_add_named_profiles(tmp_path: Path) -> None:
    path = tmp_path / "faults.json"
    path.write_text(json.dumps({"version": 1, "profiles": [
        {"name": "my-flaky", "description": "Mine", "faults": [{"kind": "rate_limit", "probability": 0.2}]},
    ]}), encoding="utf-8")
    profiles = load_profiles(path)
    assert set(profiles) == set(BUILT_IN_PROFILES) | {"my-flaky"}
    clash = tmp_path / "clash.json"
    clash.write_text(json.dumps({"version": 1, "profiles": [
        {"name": "chaos", "faults": [{"kind": "rate_limit", "probability": 0.2}]},
    ]}), encoding="utf-8")
    with pytest.raises(ValueError, match="built-in"):
        load_profiles(clash)


def test_unknown_active_profiles_fail_at_startup() -> None:
    from runweave.app import create_app
    from runweave.core.settings import Settings

    with pytest.raises(ValueError, match="fault profile"):
        create_app(Settings(proxy_fault_profile="nope"), agents=[])


def always(kind: str, **params: Any) -> dict[str, Any]:
    return {"proxy_fault_profile": "custom", "proxy_fault_inline": [{"kind": kind, "probability": 1, **params}]}


@pytest.mark.asyncio
async def test_rate_limit_faults_answer_429_with_retry_after_and_cost_nothing() -> None:
    async with proxy_session(**always("rate_limit", retry_after=7)) as s:
        chat = await s.client.post("/llm/v1/chat/completions", json=CHAT, headers=s.openai())
        claude = await s.client.post("/llm/v1/messages", json=MESSAGES, headers=s.anthropic())
        state = await s.ledger.read()
    assert chat.status_code == 429 and chat.headers["retry-after"] == "7"
    assert chat.json()["error"]["code"] == "rate_limit_exceeded"
    assert claude.status_code == 429 and claude.json()["error"]["type"] == "rate_limit_error"
    assert (state.requests, state.input_tokens) == (0, 0)
    assert [status for status, _ in s.steps.details()] == ["failed", "failed"]
    assert all("fault: rate_limit (429)" in (d or "") for _, d in s.steps.details())


@pytest.mark.asyncio
async def test_server_error_and_context_length_faults_use_provider_formats() -> None:
    async with proxy_session(**always("server_error", status=502)) as s:
        server = await s.client.post("/llm/v1/chat/completions", json=CHAT, headers=s.openai())
    async with proxy_session(**always("context_length")) as s:
        chat = await s.client.post("/llm/v1/chat/completions", json=CHAT, headers=s.openai())
        claude = await s.client.post("/llm/v1/messages", json=MESSAGES, headers=s.anthropic())
    assert server.status_code == 502 and server.json()["error"]["type"] == "server_error"
    assert chat.status_code == 400 and chat.json()["error"]["code"] == "context_length_exceeded"
    assert claude.status_code == 400
    assert claude.json()["error"]["type"] == "invalid_request_error"
    assert "too long" in claude.json()["error"]["message"]


@pytest.mark.asyncio
async def test_timeout_faults_hold_the_request_then_answer_504() -> None:
    async with proxy_session(**always("timeout", seconds=0.3)) as s:
        loop = __import__("asyncio").get_running_loop()
        started = loop.time()
        response = await s.client.post("/llm/v1/chat/completions", json=CHAT, headers=s.openai())
        elapsed = loop.time() - started
    assert response.status_code == 504 and elapsed >= 0.3


@pytest.mark.asyncio
async def test_timeout_faults_end_at_the_run_deadline() -> None:
    async with proxy_session(deadline=0.3, **always("timeout", seconds=60)) as s:
        loop = __import__("asyncio").get_running_loop()
        started = loop.time()
        await s.client.post("/llm/v1/chat/completions", json=CHAT, headers=s.openai())
        assert loop.time() - started < 5


@pytest.mark.asyncio
async def test_slow_first_token_delays_but_completes() -> None:
    async with proxy_session(**always("slow_first_token", seconds=0.3)) as s:
        loop = __import__("asyncio").get_running_loop()
        started = loop.time()
        response = await s.client.post(
            "/llm/v1/chat/completions", json={**CHAT, "stream": True}, headers=s.openai()
        )
        elapsed = loop.time() - started
    assert response.status_code == 200 and "[DONE]" in response.text and elapsed >= 0.3
    assert "fault: slow_first_token" in (s.steps.details()[0][1] or "")


@pytest.mark.asyncio
async def test_stream_disconnect_cuts_the_stream_and_keeps_the_reservation(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    async with proxy_session(**always("stream_disconnect", after_chunks=2)) as s:
        with pytest.raises((httpx.RemoteProtocolError, httpx.ReadError, RuntimeError)):
            async with s.client.stream(
                "POST", "/llm/v1/chat/completions", json={**CHAT, "stream": True}, headers=s.openai()
            ) as response:
                async for _ in response.aiter_bytes():
                    pass
        state = await s.ledger.read()
        plain = await s.client.post("/llm/v1/chat/completions", json=CHAT, headers=s.openai())
    assert state.summary().uncertain_requests == 1
    assert s.steps.details()[0][0] == "failed"
    assert plain.status_code == 200  # Non-streaming calls are unaffected.


@pytest.mark.asyncio
async def test_tool_faults_break_tool_calls() -> None:
    async with proxy_session(**always("malformed_tool_call")) as s:
        malformed = await s.client.post("/llm/v1/chat/completions", json=CHAT_TOOLS, headers=s.openai())
        untouched = await s.client.post("/llm/v1/chat/completions", json=CHAT, headers=s.openai())
    async with proxy_session(**always("unknown_tool")) as s:
        unknown = await s.client.post("/llm/v1/chat/completions", json=CHAT_TOOLS, headers=s.openai())
    call = malformed.json()["choices"][0]["message"]["tool_calls"][0]["function"]
    assert call["name"] == "lookup_weather"
    with pytest.raises(json.JSONDecodeError):
        json.loads(call["arguments"])
    assert untouched.json()["choices"][0]["message"]["content"]
    name = unknown.json()["choices"][0]["message"]["tool_calls"][0]["function"]["name"]
    assert name not in {"lookup_weather"}


@pytest.mark.asyncio
async def test_the_same_seed_gives_the_same_outcomes_end_to_end() -> None:
    async def outcomes(seed: int) -> list[int]:
        async with proxy_session(proxy_fault_profile="flaky", proxy_fault_seed=seed) as s:
            return [
                (await s.client.post("/llm/v1/chat/completions", json=CHAT, headers=s.openai())).status_code
                for _ in range(40)
            ]

    first, again, other = await outcomes(11), await outcomes(11), await outcomes(12)
    assert first == again != other
    assert {429, 200} <= set(first)
