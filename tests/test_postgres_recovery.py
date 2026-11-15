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
from runweave.budgets import BudgetExceeded, BudgetPolicy, Reservation
from runweave.core.settings import Settings
from runweave.orchestration.models import RunError, RunSnapshot, RunStatus
from runweave.security import LOCAL_PRINCIPAL, Principal, TenantQuotaExceeded
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
    await Reservation.reserve(store.ledger(claim), "fake-v1", 11, 7)
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
        legacy_id = uuid4()
        legacy_active = uuid4()
        legacy_token = uuid4()
        async with engine.begin() as conn:
            for name in (
                "0001_durable_runs.sql",
                "0002_run_principals.sql",
                "0003_run_budgets.sql",
            ):
                migration = _TEST_FILE.parents[1] / "migrations" / name
                migration_sql = await asyncio.to_thread(migration.read_text, "utf-8")
                for statement in migration_sql.split(";"):
                    if statement.strip():
                        await conn.execute(text(statement))
                if name == "0001_durable_runs.sql":
                    await conn.execute(
                        text("""
                        INSERT INTO runweave_runs
                        (id, incident, status, submitted_at, available_at, deadline_at,
                         finished_at, max_attempts)
                        VALUES (:id, '{"service":"payments","description":"Legacy"}'::jsonb,
                            'CANCELLED', now(), now(), now(), now(), 3)
                    """),
                        {"id": legacy_id},
                    )
                if name == "0002_run_principals.sql":
                    await conn.execute(
                        text("""
                        INSERT INTO runweave_runs
                        (id, incident, principal, status, submitted_at, available_at, deadline_at,
                         started_at, attempt, max_attempts, lease_token, lease_until)
                        VALUES (:id, '{"service":"payments","description":"Legacy active"}'::jsonb,
                            CAST(:principal AS jsonb), 'RUNNING', now(), now(),
                            now() + interval '30 seconds', now(), 1, 3, :token,
                            now() + interval '10 seconds')
                    """),
                        {
                            "id": legacy_active,
                            "token": legacy_token,
                            "principal": LOCAL_PRINCIPAL.model_dump_json(),
                        },
                    )
                    await conn.execute(
                        text("""
                        INSERT INTO runweave_attempts (run_id, attempt, lease_token, started_at)
                        VALUES (:id, 1, :token, now())
                    """),
                        {"id": legacy_active, "token": legacy_token},
                    )
            legacy_principal = await conn.scalar(
                text("SELECT principal FROM runweave_runs WHERE id = :id"), {"id": legacy_id}
            )
            assert Principal.model_validate(legacy_principal) == LOCAL_PRINCIPAL
            legacy_outcome = (
                (
                    await conn.execute(
                        text("SELECT status, error, budget FROM runweave_runs WHERE id = :id"),
                        {"id": legacy_active},
                    )
                )
                .mappings()
                .one()
            )
            assert legacy_outcome["status"] == "FAILED"
            assert legacy_outcome["error"]["code"] == "BUDGET_ACCOUNTING_UNAVAILABLE"
            assert legacy_outcome["budget"] is None
            assert (
                await conn.scalar(
                    text("SELECT outcome FROM runweave_attempts WHERE run_id = :id"),
                    {"id": legacy_active},
                )
                == "FAILED"
            )
            await conn.execute(text("DELETE FROM runweave_runs WHERE id = :id"), {"id": legacy_id})
            await conn.execute(
                text("DELETE FROM runweave_runs WHERE id = :id"), {"id": legacy_active}
            )
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
        budget = await store.ledger(claim).read()
        assert (budget.requests, budget.input_tokens, budget.output_tokens) == (1, 11, 7)
        stale = store.ledger(replace(claim, token=token))
        with pytest.raises(BudgetExceeded, match="Run budget"):
            await Reservation.reserve(stale, "fake-v1", 1, 1)
        with pytest.raises(BudgetExceeded):
            await Reservation(stale, next(iter(budget.calls)), 7).settle(1, 1)
        next_call = await Reservation.reserve(store.ledger(claim), "fake-v1", 10, 5)
        await next_call.settle(2, 1)
        result = IncidentResult(summary="Recovered", severity="low", evidence_ids=("fixture",))
        assert (
            await store.finish(replace(claim, token=token), RunStatus.SUCCEEDED, result=result)
            is None
        )
        assert await store.renew(claim, 3)
        completed = await store.finish(claim, RunStatus.SUCCEEDED, result=result)
        assert completed is not None and completed.result == result
        assert completed.principal == LOCAL_PRINCIPAL
        assert completed.usage is not None
        assert (
            completed.usage.requests,
            completed.usage.input_tokens,
            completed.usage.output_tokens,
            completed.usage.uncertain_requests,
        ) == (2, 13, 8, 1)
        assert await store.cancel(run_id) == completed

        owner = Principal(tenant="tenant-a", subject="alice", scopes=LOCAL_PRINCIPAL.scopes)
        stranger = Principal(tenant="tenant-b", subject="alice", scopes=LOCAL_PRINCIPAL.scopes)
        colleague = Principal(tenant="tenant-a", subject="bob", scopes=LOCAL_PRINCIPAL.scopes)

        async def scoped_admit(principal: Principal) -> RunSnapshot | None:
            return await store.admit(
                incident,
                capacity=100,
                run_seconds=30,
                attempts=3,
                history_max=100,
                principal=principal,
                tenant_limit=1,
                budget_policy=BudgetPolicy(
                    max_requests=20,
                    max_output_tokens=10,
                    input_nanos_per_token=1,
                    output_nanos_per_token=1,
                    max_cost_nanos=12,
                    price_version="db-fixture-v1",
                ),
            )

        scoped = await asyncio.gather(
            *(scoped_admit(owner) for _ in range(8)), return_exceptions=True
        )
        owned = [run for run in scoped if not isinstance(run, BaseException) and run is not None]
        assert len(owned) == 1
        assert sum(isinstance(item, TenantQuotaExceeded) for item in scoped) == 7
        owned_id = owned[0].id
        for other in (stranger, colleague):
            assert await store.get(owned_id, other) is None
            assert await store.cancel(owned_id, other) is None
        owned_snapshot = await store.get(owned_id, owner)
        assert owned_snapshot is not None and owned_snapshot.status == RunStatus.QUEUED
        foreign = await scoped_admit(stranger)
        assert foreign is not None
        assert await store.cancel(foreign.id, stranger) is not None
        assert await store.cancel(owned_id, owner) is not None
        replacement = await scoped_admit(owner)
        assert replacement is not None
        recovered_claim = await store.claim(3)
        assert recovered_claim is not None
        assert recovered_claim.snapshot.id == replacement.id
        assert recovered_claim.snapshot.principal == owner
        ledger = store.ledger(recovered_claim)
        parallel = await asyncio.gather(
            *(Reservation.reserve(ledger, "fake-v1", 2, 3) for _ in range(12)),
            return_exceptions=True,
        )
        assert sum(isinstance(item, Reservation) for item in parallel) == 2
        assert sum(isinstance(item, BudgetExceeded) for item in parallel) == 10
        charged = await ledger.read()
        assert charged.policy.price_version == "db-fixture-v1"
        assert (charged.requests, charged.input_tokens, charged.output_tokens) == (2, 4, 6)
        held = next(item for item in parallel if isinstance(item, Reservation))
        pending = next(
            item for item in parallel if isinstance(item, Reservation) and item is not held
        )
        with pytest.raises(BudgetExceeded):
            await held.settle(2, 4)
        assert (await ledger.read()).blocked
        with pytest.raises(BudgetExceeded) as overage:
            await Reservation.reserve(ledger, "fake-v1", 0, 1)
        assert overage.value.code == "PROVIDER_USAGE_EXCEEDED_RESERVATION"
        assert (
            await store.finish(
                recovered_claim,
                RunStatus.FAILED,
                error=RunError(code=overage.value.code, message="Agent execution failed"),
            )
            is not None
        )
        with pytest.raises(BudgetExceeded):
            await pending.settle(1, 1)
        assert (await ledger.read()).output_tokens == 7

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
            assert (await client.get(replacement.status_url)).status_code == 404
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
