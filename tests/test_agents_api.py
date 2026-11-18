import asyncio
import json
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from pydantic import BaseModel, Field, SecretStr

from runweave.agents.catalog import TRIAGE_AGENT, PythonAgent
from runweave.app import create_app
from runweave.core.settings import Settings
from runweave.sdk import RunContext, agent
from runweave.security import Principal


class Text(BaseModel):
    text: str = Field(min_length=1, max_length=200)
    delay: float = Field(default=0, ge=0, le=5)


class Echo(BaseModel):
    reply: str
    remaining_positive: bool


cleanup: list[str] = []


@agent(name="echo", version="1.2.0", input=Text, output=Echo, description="Echo with two steps.")
async def echo(input: Text, ctx: RunContext) -> Echo:
    async with ctx.step("Think", detail=f"length {len(input.text)}"):
        await asyncio.sleep(input.delay)
    async with ctx.step("Answer"):
        return Echo(reply=input.text, remaining_positive=ctx.remaining_seconds() > 0)


@agent(name="crash", version="1", input=Text, output=Echo)
async def crash(input: Text, ctx: RunContext) -> Echo:
    raise RuntimeError("internal detail /home/secret/path")


@agent(name="sleepy", version="1", input=Text, output=Echo)
async def sleepy(input: Text, ctx: RunContext) -> Echo:
    try:
        await asyncio.sleep(30)
    finally:
        cleanup.append(input.text)
    raise AssertionError("unreachable")


@agent(name="wrong-shape", version="1", input=Text, output=Echo)
async def wrong_shape(input: Text, ctx: RunContext) -> Echo:
    return {"reply": 3}  # type: ignore[return-value]


@agent(name="huge", version="1", input=Text, output=Echo)
async def huge(input: Text, ctx: RunContext) -> Echo:
    return Echo(reply="x" * 5000, remaining_positive=True)


@agent(name="busy", version="1", input=Text, output=Echo, max_steps=2)
async def busy(input: Text, ctx: RunContext) -> Echo:
    for index in range(3):
        async with ctx.step(f"Step {index}"):
            pass
    return Echo(reply="never", remaining_positive=True)


@agent(name="leaky", version="1", input=Text, output=Echo)
async def leaky(input: Text, ctx: RunContext) -> Echo:
    return Echo(reply="the key is protected-test-value", remaining_positive=True)


AGENTS = [echo, crash, sleepy, wrong_shape, huge, busy, leaky]


def python_agents() -> Sequence[PythonAgent]:
    return [
        PythonAgent.from_function(fn, target=f"tests.test_agents_api:{fn.__name__}")
        for fn in AGENTS
    ]


@asynccontextmanager
async def client_for(**settings: Any) -> AsyncIterator[httpx.AsyncClient]:
    config = Settings(
        worker_count=4,
        agent_output_max_bytes=2048,
        protected_values=(SecretStr("protected-test-value"),),
        **settings,
    )
    app: FastAPI = create_app(config, agents=python_agents())
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        yield client


async def submit(
    client: httpx.AsyncClient, name: str, payload: dict[str, Any], **headers: str
) -> dict[str, Any]:
    response = await client.post(
        "/v1/runs", json={"agent": name, "input": payload}, headers=headers
    )
    assert response.status_code == 202, response.text
    return dict(response.json())


async def finished(client: httpx.AsyncClient, url: str, **headers: str) -> dict[str, Any]:
    async with asyncio.timeout(5):
        while True:
            body = (await client.get(url, headers=headers)).json()
            if body["status"] in {"SUCCEEDED", "FAILED", "TIMED_OUT", "CANCELLED"}:
                return dict(body)
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_python_agent_runs_end_to_end_with_recorded_steps() -> None:
    async with client_for() as client:
        submitted = await submit(client, "echo", {"text": "hello"})
        assert (submitted["agent"], submitted["agent_version"]) == ("echo", "1.2.0")
        run = await finished(client, submitted["status_url"])
    assert run["status"] == "SUCCEEDED"
    assert run["result"] == {"reply": "hello", "remaining_positive": True}
    assert run["input"] == {"text": "hello", "delay": 0}
    assert [(step["title"], step["status"]) for step in run["steps"]] == [
        ("Think", "succeeded"),
        ("Answer", "succeeded"),
    ]
    assert run["steps"][0]["detail"] == "length 5"
    assert run["steps"][0]["duration_ms"] >= 0


@pytest.mark.asyncio
async def test_legacy_incident_body_still_runs_the_built_in_triage_agent() -> None:
    async with client_for() as client:
        response = await client.post(
            "/v1/runs", json={"service": "payments", "description": "Latency"}
        )
        assert response.status_code == 202
        run = await finished(client, response.json()["status_url"])
    assert run["agent"] == TRIAGE_AGENT
    assert run["result"]["evidence_ids"] == ["mock-health:payments", "mock-runbook:triage-v1"]
    assert {step["title"] for step in run["steps"]} >= {"Model decision", "Tool call"}


@pytest.mark.asyncio
async def test_unknown_agents_and_invalid_input_are_rejected_without_echoing_values() -> None:
    async with client_for() as client:
        unknown = await client.post("/v1/runs", json={"agent": "nobody", "input": {}})
        invalid = await client.post(
            "/v1/runs", json={"agent": "echo", "input": {"text": "<b>" * 100}}
        )
        shape = await client.post(
            "/v1/runs", json={"agent": "echo", "input": ["not", "an", "object"]}
        )
    assert unknown.status_code == 422
    assert unknown.json()["detail"]["code"] == "UNKNOWN_AGENT"
    assert invalid.status_code == 422
    detail = invalid.json()["detail"]
    assert detail["code"] == "INVALID_INPUT"
    assert detail["errors"][0]["loc"] == ["text"]
    assert "<b>" not in invalid.text
    assert shape.status_code == 422


@pytest.mark.asyncio
async def test_request_bodies_are_bounded_before_parsing() -> None:
    async with client_for(run_request_max_bytes=4096) as client:
        response = await client.post(
            "/v1/runs",
            content=json.dumps({"agent": "echo", "input": {"text": "x" * 8000}}),
            headers={"Content-Type": "application/json"},
        )
    assert response.status_code == 413
    assert response.json()["detail"]["code"] == "INPUT_TOO_LARGE"


@pytest.mark.asyncio
async def test_failures_are_isolated_classified_and_sanitized() -> None:
    async with client_for() as client:
        runs = [
            await submit(client, name, {"text": name})
            for name in ("crash", "echo", "wrong-shape", "huge", "busy", "leaky")
        ]
        results = {run["agent"]: await finished(client, run["status_url"]) for run in runs}
    assert results["echo"]["status"] == "SUCCEEDED"
    codes = {
        name: (run["status"], run["error"]["code"]) for name, run in results.items() if run["error"]
    }
    assert codes == {
        "crash": ("FAILED", "RUN_FAILED"),
        "wrong-shape": ("FAILED", "AGENT_OUTPUT_INVALID"),
        "huge": ("FAILED", "AGENT_OUTPUT_TOO_LARGE"),
        "busy": ("FAILED", "AGENT_STEP_LIMIT"),
        "leaky": ("FAILED", "RUN_FAILED"),
    }
    assert "secret/path" not in json.dumps(results)
    assert "protected-test-value" not in json.dumps(results)
    assert len(results["busy"]["steps"]) == 2


@pytest.mark.asyncio
async def test_run_deadline_applies_to_python_agents() -> None:
    async with client_for(run_timeout_seconds=0.2) as client:
        run = await finished(
            client, (await submit(client, "echo", {"text": "slow", "delay": 2}))["status_url"]
        )
    assert run["status"] == "TIMED_OUT"
    assert run["error"]["code"] == "RUN_DEADLINE_EXCEEDED"
    assert run["steps"][0]["status"] == "failed"


@pytest.mark.asyncio
async def test_cancellation_reaches_agent_cleanup() -> None:
    cleanup.clear()
    async with client_for() as client:
        submitted = await submit(client, "sleepy", {"text": "cancel-me"})
        async with asyncio.timeout(2):
            while True:
                if (await client.get(submitted["status_url"])).json()["status"] == "RUNNING":
                    break
                await asyncio.sleep(0.01)
        cancelled = (await client.post(f"{submitted['status_url']}/cancel")).json()
        assert cancelled["status"] == "CANCELLED"
        async with asyncio.timeout(2):
            while True:
                if cleanup:
                    break
                await asyncio.sleep(0.01)
    assert cleanup == ["cancel-me"]


@pytest.mark.asyncio
async def test_agents_endpoint_describes_every_registered_agent() -> None:
    async with client_for() as client:
        response = await client.get("/v1/agents")
    assert response.status_code == 200
    agents = {item["name"]: item for item in response.json()["agents"]}
    assert agents[TRIAGE_AGENT]["kind"] == "built-in"
    assert agents[TRIAGE_AGENT]["default"] is True
    assert agents["echo"]["version"] == "1.2.0"
    assert agents["echo"]["target"] == "tests.test_agents_api:echo"
    assert agents["echo"]["input_schema"]["properties"]["text"]["maxLength"] == 200
    assert agents["busy"]["max_steps"] == 2


@pytest.mark.asyncio
async def test_run_list_is_owner_scoped_filtered_and_bounded() -> None:
    identities = {
        "synthetic-alice-token": Principal(
            tenant="acme",
            subject="alice",
            scopes=frozenset({"runs:submit", "runs:read", "runs:cancel"}),
        ),
        "synthetic-bob-token": Principal(
            tenant="acme",
            subject="bob",
            scopes=frozenset({"runs:submit", "runs:read", "runs:cancel"}),
        ),
    }
    async with client_for(auth_mode="test", auth_test_identities=identities) as client:
        alice = {"Authorization": "Bearer synthetic-alice-token"}
        bob = {"Authorization": "Bearer synthetic-bob-token"}
        first = await submit(client, "echo", {"text": "one"}, **alice)
        second = await submit(client, "crash", {"text": "two"}, **alice)
        await submit(client, "echo", {"text": "bob"}, **bob)
        await finished(client, first["status_url"], **alice)
        await finished(client, second["status_url"], **alice)
        listing = (await client.get("/v1/runs", headers=alice)).json()
        filtered = (await client.get("/v1/runs?agent=echo&status=SUCCEEDED", headers=alice)).json()
        limited = (await client.get("/v1/runs?limit=1", headers=alice)).json()
        too_many = await client.get("/v1/runs?limit=1000", headers=alice)
    assert [run["id"] for run in listing["runs"]] == [second["id"], first["id"]]
    assert not {"steps", "result", "input"} & set(listing["runs"][0])
    assert [run["id"] for run in filtered["runs"]] == [first["id"]]
    assert len(limited["runs"]) == 1
    assert too_many.status_code == 422


@pytest.mark.asyncio
async def test_event_stream_sends_snapshots_until_the_run_finishes() -> None:
    async with client_for() as client:
        submitted = await submit(client, "echo", {"text": "stream", "delay": 0.05})
        response = await client.get(f"{submitted['status_url']}/events")
        missing = await client.get("/v1/runs/00000000-0000-4000-8000-000000000000/events")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-store"
    events = [
        json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")
    ]
    assert events[0]["id"] == submitted["id"]
    assert events[-1]["status"] == "SUCCEEDED"
    assert len(events[-1]["steps"]) == 2
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_event_stream_subscribers_are_bounded() -> None:
    async with client_for(event_stream_max_subscribers=1) as client:
        slow = await submit(client, "echo", {"text": "hold", "delay": 0.5})
        holder = asyncio.create_task(client.get(f"{slow['status_url']}/events"))
        await asyncio.sleep(0.1)
        rejected = await client.get(f"{slow['status_url']}/events")
        await holder
    assert rejected.status_code == 503
    assert rejected.json()["detail"]["code"] == "EVENT_STREAMS_BUSY"
    assert rejected.headers["Retry-After"] == "1"
