import asyncio
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import text
from test_postgres_checkpoints import Database, FixtureHealth, engine_for
from test_postgres_checkpoints import database as database

from runweave.agents.contracts import AgentExecutor, IncidentRequest, IncidentResult
from runweave.agents.fake import FakeAgentModel
from runweave.agents.runner import AgentRunner
from runweave.agents.triage import TriageSteps
from runweave.app import create_app
from runweave.approvals import (
    ApprovalDecision,
    ApprovalError,
    ApprovalSnapshot,
    AwaitingApproval,
    ticket_scope,
)
from runweave.budgets import budget_scope
from runweave.checkpoints import Workflow, checkpoint_scope
from runweave.core.settings import Settings
from runweave.orchestration.models import RunStatus
from runweave.security import ALL_SCOPES, LOCAL_PRINCIPAL, ContentPolicy, Principal, principal_scope
from runweave.storage.approvals import PostgresTicketGate, SandboxTicketEffect
from runweave.storage.postgres import Claim
from runweave.telemetry import Telemetry
from runweave.tools.registry import ToolRegistry

INCIDENT = IncidentRequest(
    service="payments", description="Synthetic gated action", create_ticket=True
)
TOKEN = "synthetic-approval-owner"
_FILE = Path(__file__).resolve()


def runner(db: Database, workflow: Workflow) -> AgentExecutor:
    health = FixtureHealth(db.engine)
    if workflow == "baseline":
        return AgentRunner(FakeAgentModel(), health, telemetry=Telemetry())
    from runweave.agents.workflow import PydanticDecisionModel, PydanticWorkflow

    return PydanticWorkflow(
        TriageSteps(PydanticDecisionModel(FakeAgentModel()), ToolRegistry(health), Telemetry(), 5),
        7,
        2048,
    )


async def execute(db: Database, claim: Claim, workflow: Workflow) -> IncidentResult:
    gate = PostgresTicketGate(
        db.engine,
        claim,
        ttl=3600,
        limit=100,
        authorize=lambda: claim.snapshot.principal,
        content_policy=ContentPolicy(),
    )
    with (
        budget_scope(db.store.ledger(claim)),
        checkpoint_scope(db.store.checkpoints(claim)),
        principal_scope(claim.snapshot.principal),
        ticket_scope(gate),
    ):
        return await runner(db, workflow).run(claim.incident)


async def waiting(db: Database, workflow: Workflow = "baseline", *, limit: int = 100) -> UUID:
    submitted = await db.store.admit(
        INCIDENT, capacity=10, run_seconds=60, attempts=3, history_max=100, workflow=workflow
    )
    assert submitted is not None
    claim = await db.store.claim(10)
    assert claim is not None and claim.snapshot.id == submitted.id
    gate = PostgresTicketGate(
        db.engine,
        claim,
        ttl=3600,
        limit=limit,
        authorize=lambda: LOCAL_PRINCIPAL,
        content_policy=ContentPolicy(),
    )
    with (
        budget_scope(db.store.ledger(claim)),
        checkpoint_scope(db.store.checkpoints(claim)),
        principal_scope(LOCAL_PRINCIPAL),
        ticket_scope(gate),
    ):
        with pytest.raises(AwaitingApproval):
            await runner(db, workflow).run(INCIDENT)
    return submitted.id


async def view(db: Database, run_id: UUID) -> ApprovalSnapshot:
    result = await db.store.approvals().get(run_id, LOCAL_PRINCIPAL)
    assert result is not None
    return result


def decision(snapshot: ApprovalSnapshot, value: str = "approve") -> ApprovalDecision:
    return ApprovalDecision.model_validate(
        {
            "approval_id": snapshot.id,
            "call_id": snapshot.call.id,
            "argument_digest": snapshot.argument_digest,
            "policy_version": snapshot.policy_version,
            "expires_at": snapshot.expires_at,
            "decision": value,
        }
    )


@asynccontextmanager
async def client_for(
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
    workflow: Workflow = "baseline",
    settings: Settings | None = None,
) -> AsyncIterator[httpx.AsyncClient]:
    monkeypatch.setattr(
        "runweave.core.runtime.postgres_engine", lambda settings: engine_for(db.dsn, db.schema)
    )
    config = settings or Settings(
        database_url=SecretStr(db.dsn),
        agent_workflow=workflow,
        worker_count=1,
        db_poll_seconds=0.05,
        lease_seconds=3,
    )
    app = create_app(config)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        yield client


async def completed(client: httpx.AsyncClient, run_id: UUID) -> dict[str, object]:
    async with asyncio.timeout(10):
        while True:
            result = (await client.get(f"/v1/runs/{run_id}")).json()
            if result["finished_at"]:
                return cast(dict[str, object], result)
            await asyncio.sleep(0.02)


async def tickets(db: Database) -> int:
    async with db.engine.connect() as conn:
        return int(await conn.scalar(text("SELECT count(*) FROM runweave_sandbox_tickets")) or 0)


@pytest.mark.parametrize("workflow", ["baseline", "pydanticai"])
async def test_approval_survives_worker_process_exit_and_resumes(
    database: Database,
    workflow: Workflow,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    submitted = await database.store.admit(
        INCIDENT, capacity=10, run_seconds=60, attempts=3, history_max=100, workflow=workflow
    )
    assert submitted is not None
    await child_exit(database, workflow, "waiting")
    snapshot = await view(database, submitted.id)
    assert snapshot.status == "pending" and await tickets(database) == 0
    async with database.engine.connect() as conn:
        row = (
            (
                await conn.execute(
                    text("SELECT status, lease_token, checkpoint_seq, budget FROM runweave_runs")
                )
            )
            .mappings()
            .one()
        )
        assert row["status"] == "WAITING_APPROVAL" and row["lease_token"] is None
        assert row["checkpoint_seq"] == 5 and row["budget"]["requests"] == 3
    async with client_for(database, monkeypatch, workflow) as client:
        got = await client.get(f"/v1/runs/{submitted.id}/approval")
        assert got.status_code == 200
        approved = await client.post(
            f"/v1/runs/{submitted.id}/approval", json=decision(snapshot).model_dump(mode="json")
        )
        assert approved.status_code == 200
        result = await completed(client, submitted.id)
        assert result["status"] == "SUCCEEDED", result
        assert cast(dict[str, object], result["usage"])["requests"] == 3
        repeat = await client.post(
            f"/v1/runs/{submitted.id}/approval", json=decision(snapshot).model_dump(mode="json")
        )
        assert repeat.status_code == 200
        assert repeat.json()["status"] == "executed" and repeat.json()["ticket_id"]
    assert await tickets(database) == 1


@pytest.mark.parametrize("value", ["approve", "reject"])
async def test_duplicate_and_conflicting_decisions_are_serialized(
    database: Database, value: str
) -> None:
    run_id = await waiting(database)
    proposal = await view(database, run_id)
    service = database.store.approvals()
    results = await asyncio.gather(
        *(service.decide(run_id, LOCAL_PRINCIPAL, decision(proposal, value)) for _ in range(5))
    )
    assert all(result is not None and result.decided_by == "developer" for result in results)
    opposite = "reject" if value == "approve" else "approve"
    with pytest.raises(ApprovalError) as error:
        await service.decide(run_id, LOCAL_PRINCIPAL, decision(proposal, opposite))
    assert error.value.code == "APPROVAL_DECISION_CONFLICT"
    assert await tickets(database) == 0
    run = await database.store.get(run_id)
    assert run is not None and run.status.value == ("QUEUED" if value == "approve" else "FAILED")


@pytest.mark.parametrize(
    "field,value",
    [
        ("approval_id", str(uuid4())),
        ("call_id", "other"),
        ("argument_digest", "0" * 64),
        ("policy_version", "other"),
        ("expires_at", "2030-01-01T00:00:00Z"),
    ],
)
async def test_decision_binds_every_field(database: Database, field: str, value: str) -> None:
    run_id = await waiting(database)
    proposal = await view(database, run_id)
    payload = decision(proposal).model_dump(mode="json")
    payload[field] = value
    with pytest.raises(ApprovalError):
        await database.store.approvals().decide(
            run_id, LOCAL_PRINCIPAL, ApprovalDecision.model_validate(payload)
        )
    assert (await view(database, run_id)).status == "pending"
    assert await tickets(database) == 0


@pytest.mark.parametrize("approved", [False, True])
async def test_cancel_prevents_waiting_or_approved_effect(
    database: Database, approved: bool
) -> None:
    run_id = await waiting(database)
    proposal = await view(database, run_id)
    if approved:
        await database.store.approvals().decide(run_id, LOCAL_PRINCIPAL, decision(proposal))
    await database.store.cancel(run_id, LOCAL_PRINCIPAL)
    assert (await view(database, run_id)).status == "cancelled"
    assert await database.store.claim(3) is None
    assert await tickets(database) == 0


@pytest.mark.parametrize("approved", [False, True])
async def test_expiry_prevents_effect(database: Database, approved: bool) -> None:
    run_id = await waiting(database)
    proposal = await view(database, run_id)
    if approved:
        await database.store.approvals().decide(run_id, LOCAL_PRINCIPAL, decision(proposal))
    async with database.engine.begin() as conn:
        await conn.execute(
            text("""
            UPDATE runweave_approvals SET created_at = now() - interval '2 hours',
                expires_at = now() - interval '1 second'
        """)
        )
    await database.store.maintain(retry_delay=0, history_ttl=3600, history_max=100)
    run = await database.store.get(run_id)
    assert run is not None and run.error is not None and run.error.code == "APPROVAL_EXPIRED"
    assert (await view(database, run_id)).status == "expired"
    assert await database.store.claim(3) is None and await tickets(database) == 0


async def test_waiting_time_is_separate_from_active_budget(
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = await waiting(database)
    async with database.engine.begin() as conn:
        await conn.execute(
            text("""
            UPDATE runweave_runs SET deadline_at = now() - interval '5 seconds',
                approval_wait_started_at = now() - interval '10 seconds'
        """)
        )
    await database.store.maintain(retry_delay=0, history_ttl=3600, history_max=100)
    run = await database.store.get(run_id)
    assert run is not None and run.status == RunStatus.WAITING_APPROVAL
    await database.store.approvals().decide(
        run_id, LOCAL_PRINCIPAL, decision(await view(database, run_id))
    )
    async with client_for(database, monkeypatch) as client:
        assert (await completed(client, run_id))["status"] == "SUCCEEDED"
    async with database.engine.connect() as conn:
        seconds = await conn.scalar(text("SELECT approval_wait_seconds FROM runweave_runs"))
        assert seconds is not None and seconds >= 10


@pytest.mark.parametrize("mode", ["scope", "submit", "revoked", "expired", "removed"])
async def test_current_authorization_rechecked_before_effect(
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    from datetime import UTC, datetime, timedelta

    run_id = await waiting(database)
    await database.store.approvals().decide(
        run_id, LOCAL_PRINCIPAL, decision(await view(database, run_id))
    )
    owner = LOCAL_PRINCIPAL
    if mode == "scope":
        owner = owner.model_copy(update={"scopes": owner.scopes - {"tools:create_ticket"}})
    if mode == "submit":
        owner = owner.model_copy(update={"scopes": owner.scopes - {"runs:submit"}})
    if mode == "expired":
        owner = owner.model_copy(update={"expires_at": datetime.now(UTC) - timedelta(seconds=1)})
    settings = Settings(
        database_url=SecretStr(database.dsn),
        auth_mode="test",
        auth_test_identities={TOKEN: owner},
        worker_count=1,
        db_poll_seconds=0.05,
    )
    if mode == "revoked":
        settings.auth_revoked_subjects = frozenset({("local", "developer")})
    if mode == "removed":
        settings.auth_test_identities[TOKEN] = Principal(
            tenant="other", subject="other", scopes=ALL_SCOPES
        )
    async with client_for(database, monkeypatch, settings=settings):
        async with asyncio.timeout(10):
            while True:
                run = await database.store.get(run_id)
                if run is not None and run.finished_at is not None:
                    break
                await asyncio.sleep(0.02)
        assert run.status == RunStatus.FAILED
    assert await tickets(database) == 0


async def test_pending_capacity_is_bounded(database: Database) -> None:
    await waiting(database, limit=1)
    with pytest.raises(ApprovalError) as error:
        await waiting(database, limit=1)
    assert error.value.code == "APPROVAL_CAPACITY_EXCEEDED"
    assert await tickets(database) == 0


@pytest.mark.parametrize("workflow", ["baseline", "pydanticai"])
async def test_sandbox_commit_survives_process_exit_without_duplicate(
    database: Database,
    workflow: Workflow,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = await waiting(database, workflow)
    proposal = await view(database, run_id)
    await database.store.approvals().decide(run_id, LOCAL_PRINCIPAL, decision(proposal))
    await child_exit(database, workflow, "effect")
    assert await tickets(database) == 1
    async with database.engine.begin() as conn:
        await conn.execute(
            text("UPDATE runweave_runs SET lease_until = now() - interval '1 second'")
        )
    await database.store.maintain(retry_delay=0, history_ttl=3600, history_max=100)
    async with client_for(database, monkeypatch, workflow) as client:
        result = await completed(client, run_id)
        assert result["status"] == "SUCCEEDED", result
        assert cast(dict[str, object], result["usage"])["requests"] == 3
    assert await tickets(database) == 1
    async with database.engine.connect() as conn:
        assert (
            await conn.scalar(
                text("SELECT count(*) FROM runweave_operations WHERE status != 'committed'")
            )
            == 0
        )


async def test_api_wait_releases_only_worker_and_enforces_owner_and_scope(
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = LOCAL_PRINCIPAL
    settings = Settings(
        database_url=SecretStr(database.dsn),
        auth_mode="test",
        auth_test_identities={
            TOKEN: owner,
            "synthetic-foreign-token": Principal(
                tenant="other", subject="other", scopes=ALL_SCOPES
            ),
            "synthetic-same-tenant-token": Principal(
                tenant="local", subject="other", scopes=ALL_SCOPES
            ),
            "synthetic-no-approval-token": owner.model_copy(
                update={"scopes": ALL_SCOPES - {"approvals:decide"}}
            ),
        },
        worker_count=1,
        db_poll_seconds=0.05,
    )
    async with client_for(database, monkeypatch, settings=settings) as client:
        client.headers["Authorization"] = f"Bearer {TOKEN}"
        submitted = await client.post("/v1/runs", json=INCIDENT.model_dump(mode="json"))
        assert submitted.status_code == 202
        run_id = UUID(submitted.json()["id"])
        async with asyncio.timeout(10):
            while True:
                if (await client.get(f"/v1/runs/{run_id}")).json()["status"] == "WAITING_APPROVAL":
                    break
                await asyncio.sleep(0.02)
        ordinary = await client.post(
            "/v1/runs", json={"service": "payments", "description": "Other run"}
        )
        assert (await completed(client, UUID(ordinary.json()["id"])))["status"] == "SUCCEEDED"
        proposal = await view(database, run_id)
        payload = decision(proposal).model_dump(mode="json")
        for token in ("synthetic-foreign-token", "synthetic-same-tenant-token"):
            headers = {"Authorization": f"Bearer {token}"}
            assert (
                await client.get(f"/v1/runs/{run_id}/approval", headers=headers)
            ).status_code == 404
            assert (
                await client.post(f"/v1/runs/{run_id}/approval", json=payload, headers=headers)
            ).status_code == 404
        assert (
            await client.post(
                f"/v1/runs/{run_id}/approval",
                json=payload,
                headers={"Authorization": "Bearer synthetic-no-approval-token"},
            )
        ).status_code == 403
        assert (
            await client.post(
                f"/v1/runs/{run_id}/approval", json={**payload, "arguments": {"title": "Changed"}}
            )
        ).status_code == 422
        assert (await client.post(f"/v1/runs/{run_id}/approval", json=payload)).status_code == 200
        assert (await completed(client, run_id))["status"] == "SUCCEEDED"
    assert await tickets(database) == 1


@pytest.mark.parametrize("mode", ["arguments", "messages", "policy"])
async def test_execution_rejects_changed_approval_state(
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    run_id = await waiting(database)
    await database.store.approvals().decide(
        run_id, LOCAL_PRINCIPAL, decision(await view(database, run_id))
    )
    async with database.engine.begin() as conn:
        if mode == "arguments":
            await conn.execute(
                text("""
                UPDATE runweave_approvals SET call = jsonb_set(call, '{arguments}',
                    to_jsonb('{"ticket":{"service":"other","title":"Changed","severity":"high"}}'::text))
            """)
            )
        elif mode == "messages":
            await conn.execute(text("UPDATE runweave_approvals SET messages = '[]'::jsonb"))
        else:
            await conn.execute(text("UPDATE runweave_approvals SET policy_version = 'other'"))
    async with client_for(database, monkeypatch) as client:
        assert (await completed(client, run_id))["status"] == "FAILED"
    assert await tickets(database) == 0


async def test_opposing_concurrent_decisions_have_one_winner(database: Database) -> None:
    run_id = await waiting(database)
    proposal = await view(database, run_id)
    outcomes = await asyncio.gather(
        *(
            database.store.approvals().decide(run_id, LOCAL_PRINCIPAL, decision(proposal, value))
            for value in ("approve", "reject")
        ),
        return_exceptions=True,
    )
    assert sum(isinstance(result, ApprovalSnapshot) for result in outcomes) == 1
    assert sum(isinstance(result, ApprovalError) for result in outcomes) == 1
    assert await tickets(database) == 0


async def child_exit(db: Database, workflow: Workflow, mode: str) -> None:
    environment = {**os.environ, "TEST_RUNWEAVE_POSTGRES_SCHEMA": db.schema}
    child = await asyncio.create_subprocess_exec(
        sys.executable,
        str(_FILE),
        workflow,
        mode,
        env=environment,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(child.communicate(), 30)
    assert child.returncode == 17, stderr.decode() + stdout.decode()


async def crash_child() -> None:
    from runweave.storage.postgres import PostgresRuns

    dsn = os.environ["TEST_RUNWEAVE_POSTGRES_URL"]
    schema = os.environ["TEST_RUNWEAVE_POSTGRES_SCHEMA"]
    engine = engine_for(dsn, schema)
    db = Database(dsn, schema, engine, PostgresRuns(engine))
    workflow = cast(Workflow, sys.argv[1])
    mode = sys.argv[2]
    # Finish imports before taking a lease, as native Windows cold imports can be slow.
    runner(db, workflow)
    if mode == "effect":
        original = SandboxTicketEffect.perform

        async def kill_after_effect(self: SandboxTicketEffect, operation_id: str):  # type: ignore[no-untyped-def]
            await original(self, operation_id)
            os._exit(17)

        SandboxTicketEffect.perform = kill_after_effect  # type: ignore[method-assign]
    claim = await db.store.claim(10)
    assert claim is not None
    try:
        await execute(db, claim, workflow)
    except AwaitingApproval:
        assert mode == "waiting"
        os._exit(17)
    raise AssertionError("Expected process termination")


if __name__ == "__main__":
    asyncio.run(crash_child())
