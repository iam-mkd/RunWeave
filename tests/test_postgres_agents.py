import asyncio
import os
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from pydantic import BaseModel, SecretStr
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

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
