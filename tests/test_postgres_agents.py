import asyncio
import json
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest
from pydantic import BaseModel, SecretStr
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from runweave.agent_effects import call_counts, duplicate_writes, operation_digest, resource_digest
from runweave.agents.catalog import TRIAGE_AGENT, PythonAgent
from runweave.app import create_app
from runweave.core.settings import Settings
from runweave.sdk import RunContext, agent
from runweave.security import LOCAL_PRINCIPAL
from runweave.storage.postgres import PostgresRuns

_DSN = "TEST_RUNWEAVE_POSTGRES_URL"
_MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"


class Item(BaseModel):
    text: str


class Done(BaseModel):
    text: str
    attempt: int


@agent(name="durable-echo", version="2.0.0", input=Item, output=Done)
async def durable_echo(input: Item, ctx: RunContext) -> Done:
    async with ctx.step("Persisted step", detail=input.text):
        await asyncio.sleep(0.01)
    return Done(text=input.text, attempt=ctx.attempt)


def engine_for(dsn: str, schema: str) -> AsyncEngine:
    return create_async_engine(
        dsn, hide_parameters=True, connect_args={"server_settings": {"search_path": schema}}
    )


@pytest.fixture
async def schema() -> AsyncIterator[tuple[str, str, AsyncEngine]]:
    dsn = os.environ.get(_DSN)
    if not dsn:
        pytest.skip(f"Set {_DSN} to run PostgreSQL agent tests")
    name = f"agents_test_{uuid4().hex}"
    admin = create_async_engine(dsn, hide_parameters=True)
    engine = engine_for(dsn, name)
    async with admin.begin() as conn:
        await conn.execute(text(f"CREATE SCHEMA {name}"))
    try:
        yield dsn, name, engine
    finally:
        await engine.dispose()
        async with admin.begin() as conn:
            await conn.execute(text(f"DROP SCHEMA {name} CASCADE"))
        await admin.dispose()


async def apply(engine: AsyncEngine, *, until: str | None = None, start: str | None = None) -> None:
    paths = await asyncio.to_thread(lambda: sorted(_MIGRATIONS.glob("*.sql")))
    async with engine.begin() as conn:
        for path in paths:
            if (start and path.name < start) or (until and path.name > until):
                continue
            sql = await asyncio.to_thread(path.read_text, "utf-8")
            for statement in sql.split(";"):
                if statement.strip():
                    await conn.execute(text(statement))


@pytest.mark.asyncio
async def test_migration_0007_preserves_existing_runs_as_triage(
    schema: tuple[str, str, AsyncEngine],
) -> None:
    _, _, engine = schema
    await apply(engine, until="0006_scoped_memory.sql")
    legacy = uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text("""
            INSERT INTO runweave_runs (id, incident, principal, status, submitted_at,
                available_at, deadline_at, finished_at, max_attempts)
            VALUES (:id, '{"service":"payments","description":"Legacy"}'::jsonb,
                CAST(:principal AS jsonb), 'CANCELLED', now(), now(), now(), now(), 3)
        """),
            {"id": legacy, "principal": LOCAL_PRINCIPAL.model_dump_json()},
        )
    await apply(engine, start="0007_generic_agents.sql")
    run = await PostgresRuns(engine).get(legacy)
    assert run is not None
    assert (run.agent, run.agent_version, run.steps) == (TRIAGE_AGENT, None, ())
    with pytest.raises(DBAPIError):
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE runweave_runs SET agent = 'Not Valid' WHERE id = :id"), {"id": legacy}
            )


@pytest.mark.asyncio
async def test_durable_python_agent_persists_steps_lists_runs_and_recovers(
    schema: tuple[str, str, AsyncEngine], monkeypatch: pytest.MonkeyPatch
) -> None:
    dsn, name, engine = schema
    await apply(engine)
    monkeypatch.setattr(
        "runweave.core.runtime.postgres_engine", lambda settings: engine_for(dsn, name)
    )
    settings = Settings(
        database_url=SecretStr(dsn), worker_count=1, db_poll_seconds=0.05, lease_seconds=1
    )
    agents = [
        PythonAgent.from_function(durable_echo, target="tests.test_postgres_agents:durable_echo")
    ]

    store = PostgresRuns(engine)
    app = create_app(settings, agents=agents)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        response = await client.post(
            "/v1/runs", json={"agent": "durable-echo", "input": {"text": "durable"}}
        )
        assert response.status_code == 202, response.text
        submitted = response.json()
        assert submitted["agent"] == "durable-echo"
        events = (await client.get(f"{submitted['status_url']}/events")).text
        run = (await client.get(submitted["status_url"])).json()
        listing = (await client.get("/v1/runs?agent=durable-echo")).json()
    assert run["status"] == "SUCCEEDED"
    assert run["result"] == {"text": "durable", "attempt": 1}
    assert run["input"] == {"text": "durable"}
    assert [(step["title"], step["status"], step["attempt"]) for step in run["steps"]] == [
        ("Persisted step", "succeeded", 1)
    ]
    assert '"status":"SUCCEEDED"' in events
    assert [item["id"] for item in listing["runs"]] == [submitted["id"]]

    # A lost lease re-runs an SDK agent from the start: at-least-once, attempt 2.
    response_id = uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text("""
            INSERT INTO runweave_runs (id, incident, agent, agent_version, status, submitted_at,
                available_at, deadline_at, max_attempts, attempt, started_at, lease_token,
                lease_until, principal, budget)
            SELECT :id, '{"text":"recovered"}'::jsonb, 'durable-echo', '2.0.0', 'RUNNING', now(),
                now(), now() + interval '30 seconds', 3, 1, now(), gen_random_uuid(),
                now() - interval '1 second', principal, budget FROM runweave_runs LIMIT 1
        """),
            {"id": response_id},
        )
    app = create_app(settings, agents=agents)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        async with asyncio.timeout(10):
            while True:
                recovered = (await client.get(f"/v1/runs/{response_id}")).json()
                if recovered["status"] == "SUCCEEDED":
                    break
                await asyncio.sleep(0.05)
    assert recovered["result"] == {"text": "recovered", "attempt": 2}
    assert await store.get(response_id) is not None


class Refund(BaseModel):
    order: str
    keyed: bool


class Refunded(BaseModel):
    refund: str
    attempt: int


REFUNDS: dict[str, list[str]] = {}


async def issue_refund(order: str, key: str | None) -> str:
    """A downstream that deduplicates by idempotency key; unkeyed calls always pay again."""
    issued = REFUNDS.setdefault(order, [])
    if key is None or not issued:
        issued.append(f"{order}-{len(issued) + 1}")
    return issued[-1] if key is None else issued[0]


@agent(name="durable-refund", version="1.0.0", input=Refund, output=Refunded)
async def durable_refund(input: Refund, ctx: RunContext) -> Refunded:
    key = f"refund-{input.order}" if input.keyed else None
    refund = await ctx.effect("refund", lambda: issue_refund(input.order, key), key=key)
    return Refunded(refund=refund, attempt=ctx.attempt)


@pytest.mark.asyncio
@pytest.mark.parametrize("keyed", [True, False])
async def test_effects_are_checked_across_a_durable_re_run(
    schema: tuple[str, str, AsyncEngine], monkeypatch: pytest.MonkeyPatch, keyed: bool
) -> None:
    dsn, name, engine = schema
    await apply(engine)
    monkeypatch.setattr(
        "runweave.core.runtime.postgres_engine", lambda settings: engine_for(dsn, name)
    )
    settings = Settings(
        database_url=SecretStr(dsn), worker_count=1, db_poll_seconds=0.05, lease_seconds=1
    )
    agents = [
        PythonAgent.from_function(
            durable_refund, target="tests.test_postgres_agents:durable_refund"
        )
    ]
    order = f"order-{uuid4().hex[:8]}"
    key = f"refund-{order}" if keyed else None
    first = await issue_refund(order, key)  # attempt 1 committed this, then its worker died
    step = {
        "index": 0, "attempt": 1, "title": "refund", "status": "succeeded",
        "detail": "write committed", "started_at": "2026-11-25T00:00:00Z",
        "finished_at": "2026-11-25T00:00:01Z", "duration_ms": 1.0, "kind": "effect",
        "effect": "refund", "operation": operation_digest("refund", key),
        "resource": resource_digest(first),
    }  # fmt: skip
    app = create_app(settings, agents=agents)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        seed = await client.post(
            "/v1/runs",
            json={"agent": "durable-refund", "input": {"order": f"seed-{order}", "keyed": True}},
        )
        assert seed.status_code == 202, seed.text
        run_id = uuid4()
        async with engine.begin() as conn:
            await conn.execute(
                text("""
                INSERT INTO runweave_runs (id, incident, agent, agent_version, status,
                    submitted_at, available_at, deadline_at, max_attempts, attempt, started_at,
                    lease_token, lease_until, principal, budget, steps)
                SELECT :id, CAST(:input AS jsonb), 'durable-refund', '1.0.0', 'RUNNING', now(),
                    now(), now() + interval '30 seconds', 3, 1, now(), gen_random_uuid(),
                    now() - interval '1 second', principal, budget, CAST(:steps AS jsonb)
                FROM runweave_runs LIMIT 1
            """),
                {
                    "id": run_id,
                    "input": json.dumps({"order": order, "keyed": keyed}),
                    "steps": json.dumps([step]),
                },
            )
        async with asyncio.timeout(10):
            while True:
                run = (await client.get(f"/v1/runs/{run_id}")).json()
                if run["status"] in {"SUCCEEDED", "FAILED"}:
                    break
                await asyncio.sleep(0.05)
    assert run["status"] == "SUCCEEDED" and run["result"]["attempt"] == 2, run
    assert [(s["kind"], s["attempt"]) for s in run["steps"]] == [("effect", 1), ("effect", 2)]
    assert call_counts(run["steps"]) == {"refund": 1}
    assert duplicate_writes(run["steps"]) == ([] if keyed else ["refund"])
    assert len(REFUNDS[order]) == (1 if keyed else 2)


class Remote(BaseModel):
    text: str


@agent(name="durable-remote", version="1.0.0", input=Remote, output=Done)
async def durable_remote(input: Remote, ctx: RunContext) -> Done:
    async with ctx.step("Remote step", detail=input.text):
        await asyncio.sleep(0.01)
    return Done(text=input.text.upper(), attempt=ctx.attempt)


@pytest.mark.asyncio
async def test_durable_http_agent_persists_remote_steps_and_rejects_stale_input(
    schema: tuple[str, str, AsyncEngine], monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_server import agent_server

    dsn, name, engine = schema
    await apply(engine)
    monkeypatch.setattr(
        "runweave.core.runtime.postgres_engine", lambda settings: engine_for(dsn, name)
    )
    async with agent_server(durable_remote) as url:
        settings = Settings(
            database_url=SecretStr(dsn),
            worker_count=1,
            db_poll_seconds=0.05,
            lease_seconds=1,
            http_agents=(url,),
        )
        app = create_app(settings, agents=[])
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client,
        ):
            response = await client.post(
                "/v1/runs", json={"agent": "durable-remote", "input": {"text": "remote"}}
            )
            assert response.status_code == 202, response.text
            status_url = response.json()["status_url"]
            await client.get(f"{status_url}/events")
            run = (await client.get(status_url)).json()

            # Input stored before the agent's schema changed is not sent to the agent.
            stale = uuid4()
            async with engine.begin() as conn:
                await conn.execute(
                    text("""
                    INSERT INTO runweave_runs (id, incident, agent, agent_version, status,
                        submitted_at, available_at, deadline_at, max_attempts, principal, budget)
                    SELECT :id, '{"wrong":"shape"}'::jsonb, 'durable-remote', '1.0.0', 'QUEUED',
                        now(), now(), now() + interval '30 seconds', 3, principal, budget
                    FROM runweave_runs LIMIT 1
                """),
                    {"id": stale},
                )
            async with asyncio.timeout(10):
                while True:
                    rejected = (await client.get(f"/v1/runs/{stale}")).json()
                    if rejected["status"] in {"FAILED", "SUCCEEDED"}:
                        break
                    await asyncio.sleep(0.05)
    assert run["status"] == "SUCCEEDED", run
    assert run["agent"] == "durable-remote"
    assert run["result"] == {"text": "REMOTE", "attempt": 1}
    assert [(s["title"], s["status"], s["attempt"]) for s in run["steps"]] == [
        ("Remote step", "succeeded", 1)
    ]
    assert rejected["status"] == "FAILED"
    assert rejected["error"]["code"] == "AGENT_INPUT_INCOMPATIBLE"


PROXY_APP: dict[str, Any] = {}


class Prompt(BaseModel):
    text: str


class Reply(BaseModel):
    text: str


@agent(name="durable-proxy", version="1", input=Prompt, output=Reply)
async def durable_proxy(input: Prompt, ctx: RunContext) -> Reply:
    assert ctx.proxy is not None
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=PROXY_APP["app"]), base_url="http://test"
    ) as http:
        response = await http.post(
            "/llm/v1/chat/completions",
            json={"model": "m", "messages": [{"role": "user", "content": input.text}]},
            headers={"Authorization": f"Bearer {ctx.proxy.token}"},
        )
    response.raise_for_status()
    return Reply(text=response.json()["choices"][0]["message"]["content"])


@pytest.mark.asyncio
async def test_durable_runs_meter_proxied_model_calls(
    schema: tuple[str, str, AsyncEngine], monkeypatch: pytest.MonkeyPatch
) -> None:
    dsn, name, engine = schema
    await apply(engine)
    monkeypatch.setattr(
        "runweave.core.runtime.postgres_engine", lambda settings: engine_for(dsn, name)
    )
    settings = Settings(database_url=SecretStr(dsn), worker_count=1, db_poll_seconds=0.05)
    app = create_app(
        settings,
        agents=[PythonAgent.from_function(durable_proxy, target="tests.test_postgres_agents:p")],
    )
    PROXY_APP["app"] = app
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        response = await client.post(
            "/v1/runs", json={"agent": "durable-proxy", "input": {"text": "hello"}}
        )
        status_url = response.json()["status_url"]
        await client.get(f"{status_url}/events")
        run = (await client.get(status_url)).json()
    assert run["status"] == "SUCCEEDED", run
    assert run["result"]["text"] == "This is a deterministic RunWeave mock response."
    assert run["usage"]["requests"] == 1 and run["usage"]["input_tokens"] > 0
    assert [(s["title"], s["status"]) for s in run["steps"]] == [("Model call", "succeeded")]
