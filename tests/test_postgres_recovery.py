import asyncio
import os
import sys
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from runweave.agents.contracts import IncidentRequest, IncidentResult
from runweave.app import create_app
from runweave.core.settings import Settings
from runweave.orchestration.models import RunStatus
from runweave.storage.postgres import PostgresRuns

_DSN = "TEST_RUNWEAVE_POSTGRES_URL"
_SCHEMA = "TEST_RUNWEAVE_POSTGRES_SCHEMA"
_TEST_FILE = Path(__file__).resolve()


def scoped_engine(dsn: str, schema: str) -> AsyncEngine:
    return create_async_engine(
        dsn, hide_parameters=True, connect_args={"server_settings": {"search_path": schema}}
    )


async def crash_after_claim() -> None:
    store = PostgresRuns(scoped_engine(os.environ[_DSN], os.environ[_SCHEMA]))
    claim = await store.claim(1)
    assert claim is not None
    print(claim.snapshot.id, flush=True)
    os._exit(17)


@pytest.mark.asyncio
async def test_postgres_recovery_after_worker_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    dsn = os.environ.get(_DSN)
    if not dsn:
        pytest.skip(f"Set {_DSN} to run the PostgreSQL integration test")
    schema = f"runweave_test_{uuid4().hex}"
    admin = create_async_engine(dsn, hide_parameters=True)
    engine = scoped_engine(dsn, schema)
    async with admin.begin() as conn:
        await conn.execute(text(f"CREATE SCHEMA {schema}"))
    try:
        migration = _TEST_FILE.parents[1] / "migrations/0001_durable_runs.sql"
        migration_sql = await asyncio.to_thread(migration.read_text, "utf-8")
        async with engine.begin() as conn:
            for statement in migration_sql.split(";"):
                if statement.strip():
                    await conn.execute(text(statement))
        store = PostgresRuns(engine)
        incident = IncidentRequest(service="payments", description="Synthetic restart test")

        async def admit() -> object:
            return await store.admit(
                incident,
                capacity=1,
                run_seconds=30,
                attempts=3,
                history_max=10,
            )

        admissions = await asyncio.gather(admit(), admit())
        assert sum(item is not None for item in admissions) == 1
        async with engine.connect() as conn:
            run_id = (await conn.execute(text("SELECT id FROM runweave_runs"))).scalar_one()

        child = await asyncio.create_subprocess_exec(
            sys.executable,
            str(_TEST_FILE),
            env={**os.environ, _SCHEMA: schema},
            stdout=asyncio.subprocess.PIPE,
        )
        try:
            stdout, _ = await asyncio.wait_for(child.communicate(), 10)
            assert child.returncode == 17 and stdout.decode().strip() == str(run_id)
        finally:
            if child.returncode is None:
                child.kill()
                await child.wait()

        assert await store.claim(2) is None
        persisted = await store.get(run_id)
        assert persisted is not None and persisted.status == RunStatus.RUNNING
        async with engine.begin() as conn:
            token = (await conn.execute(text("SELECT lease_token FROM runweave_runs"))).scalar_one()
        async with asyncio.timeout(5):
            while True:
                await store.maintain(retry_delay=10, history_ttl=60, history_max=10)
                recovered = await store.get(run_id)
                if recovered is not None and recovered.status == RunStatus.QUEUED:
                    break
                await asyncio.sleep(0.05)
        assert await store.claim(3) is None
        async with engine.begin() as conn:
            outcome = (
                await conn.execute(text("SELECT outcome FROM runweave_attempts"))
            ).scalar_one()
            assert outcome == "LEASE_EXPIRED"
            await conn.execute(text("UPDATE runweave_runs SET available_at = clock_timestamp()"))

        claims = await asyncio.gather(store.claim(3), store.claim(3))
        assert sum(item is not None for item in claims) == 1
        claim = next(item for item in claims if item is not None)
        assert claim.snapshot.id == run_id and claim.token != token
        result = IncidentResult(summary="Recovered", severity="low", evidence_ids=("fixture",))
        assert (
            await store.finish(replace(claim, token=token), RunStatus.SUCCEEDED, result=result)
            is None
        )
        assert await store.renew(claim, 3)
        completed = await store.finish(claim, RunStatus.SUCCEEDED, result=result)
        assert completed is not None and completed.result == result
        assert await store.cancel(run_id) == completed

        monkeypatch.setattr(
            "runweave.core.runtime.postgres_engine", lambda settings: scoped_engine(dsn, schema)
        )
        monkeypatch.setenv("RUNWEAVE_DATABASE_URL", dsn)
        app = create_app(Settings(model_provider="fake", worker_count=2))
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client,
        ):
            assert (await client.get(completed.status_url)).json()["status"] == "SUCCEEDED"
            accepted = await client.post("/v1/runs", json=incident.model_dump())
            assert accepted.status_code == 202
            async with asyncio.timeout(5):
                while True:
                    fresh = (await client.get(accepted.headers["Location"])).json()
                    if fresh["status"] == "SUCCEEDED":
                        break
                    await asyncio.sleep(0.05)
            assert "mock-health:payments" in fresh["result"]["evidence_ids"]
    finally:
        await engine.dispose()
        try:
            async with admin.begin() as conn:
                await conn.execute(text(f"DROP SCHEMA {schema} CASCADE"))
        finally:
            await admin.dispose()


if __name__ == "__main__":
    asyncio.run(crash_after_claim())
