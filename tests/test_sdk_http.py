"""The Python agent-side wrapper that serves @agent functions over the HTTP contract."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import uuid4

import httpx
import pytest
from pydantic import BaseModel, Field

from runweave.http_contract import CONTRACT_VERSION, Manifest, RunResponse
from runweave.sdk import RunContext, agent
from runweave.sdk_http import create_agent_app


class Ask(BaseModel):
    text: str = Field(min_length=1, max_length=40)
    delay: float = Field(default=0, ge=0, le=30)


class Reply(BaseModel):
    reply: str
    attempt: int
    remaining_ms: int


calls: list[str] = []
cancelled: list[str] = []


@agent(name="echo", version="1.0.0", input=Ask, output=Reply, description="Echo", max_steps=3)
async def echo(input: Ask, ctx: RunContext) -> Reply:
    calls.append(input.text)
    async with ctx.step("Think", detail="token protected-value"):
        try:
            await asyncio.sleep(input.delay)
        except asyncio.CancelledError:
            cancelled.append(input.text)
            raise
    return Reply(
        reply=input.text, attempt=ctx.attempt, remaining_ms=int(ctx.remaining_seconds() * 1000)
    )


@agent(name="crash", version="1", input=Ask, output=Reply)
async def crash(input: Ask, ctx: RunContext) -> Reply:
    raise RuntimeError("internal /home/dev/secret detail")


@agent(name="shape", version="1", input=Ask, output=Reply)
async def shape(input: Ask, ctx: RunContext) -> Reply:
    return {"reply": 1}  # type: ignore[return-value]


@agent(name="steps", version="1", input=Ask, output=Reply, max_steps=1)
async def steps(input: Ask, ctx: RunContext) -> Reply:
    for _ in range(2):
        async with ctx.step("again"):
            pass
    raise AssertionError("unreachable")


@asynccontextmanager
async def client(**options: Any) -> AsyncIterator[httpx.AsyncClient]:
    app = create_agent_app(echo, crash, shape, steps, **options)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://agent") as http:
        yield http


def envelope(text: str = "hi", **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "run_id": str(uuid4()), "attempt": 1, "input": {"text": text}, "deadline_ms": 5000,
    }
    body.update(overrides)
    return body


async def post(http: httpx.AsyncClient, name: str, body: Any, key: str | None = None) -> httpx.Response:
    headers = {"Idempotency-Key": key} if key else {}
    return await http.post(f"/runweave/v1/agents/{name}/runs", json=body, headers=headers)


def test_only_async_sdk_agents_with_unique_names_can_be_served() -> None:
    async def plain(input: Ask, ctx: RunContext) -> Reply:
        raise AssertionError

    with pytest.raises(TypeError):
        create_agent_app(plain)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        create_agent_app(echo, echo)
    with pytest.raises(ValueError):
        create_agent_app()


@pytest.mark.asyncio
async def test_manifest_lists_agents_and_their_schemas() -> None:
    async with client() as http:
        response = await http.get("/runweave/v1/agents")
    assert response.status_code == 200
    body = Manifest.model_validate(response.json())
    assert body.contract == CONTRACT_VERSION
    entry = next(a for a in body.agents if a.name == "echo")
    assert (entry.version, entry.description, entry.max_steps) == ("1.0.0", "Echo", 3)
    assert entry.input_schema == Ask.model_json_schema()
    assert entry.output_schema == Reply.model_json_schema()


@pytest.mark.asyncio
async def test_run_returns_validated_output_and_redacted_steps() -> None:
    async with client(protected_values=("protected-value",)) as http:
        response = await post(http, "echo", envelope("hello", attempt=2, deadline_ms=4000))
    assert response.status_code == 200, response.text
    body = RunResponse.model_validate(response.json())
    assert body.output["reply"] == "hello" and body.output["attempt"] == 2
    assert 0 < body.output["remaining_ms"] <= 4000
    assert [(s.title, s.status) for s in body.steps] == [("Think", "succeeded")]
    assert body.steps[0].detail is not None and "protected-value" not in body.steps[0].detail


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "body", "status", "code"),
    [
        ("nobody", envelope(), 404, "UNKNOWN_AGENT"),
        ("echo", {"input": {"text": "x"}}, 400, "INVALID_REQUEST"),
        ("echo", envelope(deadline_ms=0), 400, "INVALID_REQUEST"),
        ("echo", envelope(run_id="not-a-uuid"), 400, "INVALID_REQUEST"),
        ("echo", envelope(text=""), 422, "INVALID_INPUT"),
        ("crash", envelope(), 500, "RUN_FAILED"),
        ("shape", envelope(), 500, "AGENT_OUTPUT_INVALID"),
        ("steps", envelope(), 500, "AGENT_STEP_LIMIT"),
    ],
)
async def test_failures_return_contract_error_codes_without_internal_detail(
    name: str, body: dict[str, Any], status: int, code: str
) -> None:
    async with client() as http:
        response = await post(http, name, body)
    assert response.status_code == status
    assert response.json()["error"]["code"] == code
    assert "secret" not in response.text and "Traceback" not in response.text


@pytest.mark.asyncio
async def test_invalid_input_errors_do_not_echo_values() -> None:
    async with client() as http:
        response = await post(http, "echo", envelope(text="<b>" * 30))
    assert response.status_code == 422
    assert response.json()["error"]["errors"][0]["loc"] == ["text"]
    assert "<b>" not in response.text


@pytest.mark.asyncio
async def test_non_json_and_oversized_bodies_are_rejected() -> None:
    async with client(max_body_bytes=1024) as http:
        garbage = await http.post(
            "/runweave/v1/agents/echo/runs", content=b"{nope", headers={"Content-Type": "application/json"}
        )
        large = await post(http, "echo", envelope("x", input={"text": "x" * 4000}))
    assert garbage.status_code == 400 and garbage.json()["error"]["code"] == "INVALID_REQUEST"
    assert large.status_code == 413 and large.json()["error"]["code"] == "INPUT_TOO_LARGE"


@pytest.mark.asyncio
async def test_agent_deadline_is_reported_with_its_own_code() -> None:
    async with client() as http:
        response = await post(http, "echo", envelope(input={"text": "slow", "delay": 5}, deadline_ms=100))
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "AGENT_DEADLINE_EXCEEDED"


@pytest.mark.asyncio
async def test_same_idempotency_key_runs_once() -> None:
    calls.clear()
    body = envelope(input={"text": "once", "delay": 0.2})
    key = body["run_id"]
    async with client() as http:
        first, second = await asyncio.gather(post(http, "echo", body, key), post(http, "echo", body, key))
        third = await post(http, "echo", body, key)
    assert first.status_code == second.status_code == third.status_code == 200
    assert first.json() == second.json() == third.json()
    assert calls.count("once") == 1


@pytest.mark.asyncio
async def test_reusing_a_key_for_a_different_run_is_a_conflict() -> None:
    async with client() as http:
        await post(http, "echo", envelope("a"), "shared-key")
        response = await post(http, "echo", envelope("b"), "shared-key")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"


@pytest.mark.asyncio
async def test_cancel_call_cancels_the_running_agent() -> None:
    cancelled.clear()
    body = envelope(input={"text": "cancel-me", "delay": 30})
    async with client() as http:
        running = asyncio.create_task(post(http, "echo", body, body["run_id"]))
        async with asyncio.timeout(5):
            while "cancel-me" not in calls:
                await asyncio.sleep(0.01)
        response = await http.post(f"/runweave/v1/agents/echo/runs/{body['run_id']}/cancel")
        result = await running
    assert response.status_code == 202
    assert cancelled == ["cancel-me"]
    assert result.status_code == 409 and result.json()["error"]["code"] == "RUN_CANCELLED"


@pytest.mark.asyncio
async def test_cancel_for_an_unknown_run_is_not_found() -> None:
    async with client() as http:
        response = await http.post(f"/runweave/v1/agents/echo/runs/{uuid4()}/cancel")
        malformed = await http.post("/runweave/v1/agents/echo/runs/nope/cancel")
    assert response.status_code == 404
    assert malformed.status_code in {400, 404}


@pytest.mark.asyncio
async def test_capacity_is_bounded_with_a_retryable_status() -> None:
    async with client(max_concurrency=1) as http:
        first = asyncio.create_task(post(http, "echo", envelope(input={"text": "a", "delay": 0.3})))
        await asyncio.sleep(0.05)
        busy = await post(http, "echo", envelope("b"))
        await first
    assert busy.status_code == 503
    assert busy.headers["retry-after"] == "1"
    assert busy.json()["error"]["code"] == "AGENT_BUSY"
