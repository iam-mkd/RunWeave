import asyncio
import os
import sys
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import text
from test_postgres_checkpoints import Database, FixtureHealth, engine_for, record
from test_postgres_checkpoints import database as database

from runweave.agents.contracts import (
    IncidentRequest,
    ModelDecision,
    ToolCall,
    ToolExchange,
)
from runweave.agents.coordination import ROLES, CoordinatedRunner, Progress
from runweave.agents.fake import FakeAgentModel
from runweave.agents.handoffs import ROLE_TOOLS, HandoffRequest, HandoffState, current_role
from runweave.app import create_app
from runweave.budgets import budget_scope
from runweave.checkpoints import CheckpointError, TriageCheckpoint, checkpoint_scope
from runweave.core.settings import Settings
from runweave.orchestration.models import RunSnapshot
from runweave.security import LOCAL_PRINCIPAL, principal_scope
from runweave.storage.checkpoints import PostgresCheckpoints
from runweave.storage.postgres import PostgresRuns
from runweave.telemetry import Telemetry
from runweave.tools.registry import ToolRegistry

_FILE = Path(__file__).resolve()
INCIDENT = IncidentRequest(service="payments", description="Synthetic coordinated recovery")


class Model(FakeAgentModel):
    def __init__(self, db: Database) -> None:
        self.db = db

    async def decide(
        self,
        incident: IncidentRequest,
        history: tuple[ToolExchange, ...],
        tools: tuple[dict[str, object], ...],
        completion_budget: int,
    ) -> ModelDecision:
        await record(self.db.engine, f"model:{current_role()}")
        return await super().decide(incident, history, tools, completion_budget)


class CrashStore(PostgresCheckpoints):
    mode: str

    async def save(self, checkpoint: TriageCheckpoint, expected_sequence: int) -> TriageCheckpoint:
        saved = await super().save(checkpoint, expected_sequence)
        role, _, stage = self.mode.partition(":")
        branch = next((b for r, b in saved.branches.items() if r == role), None)
        if (
            (branch is not None and branch.stage == stage)
            or (self.mode == "merge" and saved.next_node == "finish")
            or (self.mode == "done" and saved.next_node == "done")
        ):
            os._exit(17)
        return saved


async def admit(db: Database) -> RunSnapshot:
    run = await db.store.admit(
        INCIDENT, capacity=10, run_seconds=90, attempts=3, history_max=100, workflow="coordinated"
    )
    assert run is not None
    return run


@pytest.mark.parametrize(
    "mode", ["health:collect", "health:succeeded", "runbook:succeeded", "merge", "done"]
)
async def test_coordinated_worker_exit_reuses_committed_branches(
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    submitted = await admit(database)
    child = await asyncio.create_subprocess_exec(
        sys.executable,
        str(_FILE),
        mode,
        env={**os.environ, "TEST_RUNWEAVE_POSTGRES_SCHEMA": database.schema},
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(child.communicate(), 30)
        assert child.returncode == 17, stdout.decode() + stderr.decode()
    finally:
        if child.returncode is None:
            child.kill()
            await child.wait()
    async with database.engine.begin() as conn:
        checkpoint = await conn.scalar(text("SELECT checkpoint FROM runweave_runs"))
        charged = await conn.scalar(text("SELECT (budget->>'requests')::int FROM runweave_runs"))
        await conn.execute(
            text("UPDATE runweave_runs SET lease_until = now() - interval '1 second'")
        )
    await database.store.maintain(retry_delay=0, history_ttl=3600, history_max=100)
    monkeypatch.setattr(
        "runweave.core.runtime.postgres_engine",
        lambda settings: engine_for(database.dsn, database.schema),
    )
    settings = Settings(
        database_url=SecretStr(database.dsn),
        agent_workflow="coordinated",
        worker_count=1,
        db_poll_seconds=0.05,
        agent_max_steps=1,
        agent_completion_tokens=1,
    )
    app = create_app(
        settings,
        executor=CoordinatedRunner(
            Model(database),
            ToolRegistry(FixtureHealth(database.engine)),
            Telemetry(),
            max_steps=1,
            completion_budget=1,
        ),
    )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        async with asyncio.timeout(15):
            while True:
                result = (await client.get(submitted.status_url)).json()
                if result["finished_at"]:
                    break
                await asyncio.sleep(0.02)
        assert result["status"] == "SUCCEEDED", result
        assert result["usage"]["requests"] >= charged
    async with database.engine.connect() as conn:
        for role in checkpoint["branches"]:
            assert (
                await conn.scalar(
                    text("SELECT count(*) FROM fixture_events WHERE kind = :kind"),
                    {"kind": f"model:{role}"},
                )
                == 1
            )
        if checkpoint["branches"].get("health", {}).get("stage") == "succeeded":
            assert (
                await conn.scalar(text("SELECT count(*) FROM fixture_events WHERE kind = 'health'"))
                == 1
            )
        assert (
            await conn.scalar(
                text("SELECT count(*) FROM runweave_operations WHERE status != 'committed'")
            )
            == 0
        )
    if mode in ("merge", "done"):
        assert result["usage"]["requests"] == charged


async def test_branch_merges_are_atomic_and_cancelled_or_stale_claims_cannot_write(
    database: Database,
) -> None:
    await admit(database)
    claim = await database.store.claim(10)
    assert claim is not None
    persistence = database.store.checkpoints(claim)
    progress = Progress(await persistence.load(), persistence)
    states = [
        HandoffState(
            request=HandoffRequest(id=f"{progress.run_id}:{role}", role=role, service="payments"),
            stage="collect",
            call=ToolCall(
                id=f"handoff-{role}", name=ROLE_TOOLS[role], arguments='{"service":"payments"}'
            ),
        )
        for role in ROLES
    ]
    await asyncio.gather(*(progress.branch(state, None, tokens=1) for state in states))
    saved = await persistence.load()
    assert len(saved.branches) == 2 and saved.sequence == 2 and saved.steps_remaining == 5
    stale = PostgresCheckpoints(database.engine, replace(claim, token=uuid4()))
    with pytest.raises(CheckpointError):
        await Progress(saved, stale).branch(states[0], states[0])
    await database.store.cancel(claim.snapshot.id)
    with pytest.raises(CheckpointError):
        await progress.branch(states[0], states[0])


async def test_coordinated_runtime_retains_memory_and_approval_integration(
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "runweave.core.runtime.postgres_engine",
        lambda settings: engine_for(database.dsn, database.schema),
    )
    app = create_app(
        Settings(
            database_url=SecretStr(database.dsn),
            agent_workflow="coordinated",
            worker_count=1,
            db_poll_seconds=0.05,
        )
    )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        first = await client.post("/v1/runs", json=INCIDENT.model_dump(mode="json"))
        async with asyncio.timeout(15):
            while True:
                result = (await client.get(first.json()["status_url"])).json()
                if result["finished_at"]:
                    break
                await asyncio.sleep(0.02)
        assert result["status"] == "SUCCEEDED", result
        memory = await client.post(
            "/v1/memory",
            json={
                "source_run_id": result["id"],
                "evidence_id": result["result"]["evidence_ids"][0],
            },
        )
        assert (
            memory.status_code == 201 and memory.json()["provenance"]["workflow"] == "coordinated"
        )
        second = await client.post(
            "/v1/runs",
            json={**INCIDENT.model_dump(mode="json"), "memory": {}, "create_ticket": True},
        )
        url = second.json()["status_url"]
        async with asyncio.timeout(15):
            while (await client.get(url)).json()["status"] != "WAITING_APPROVAL":  # noqa: ASYNC110
                await asyncio.sleep(0.02)
        proposal = (await client.get(url + "/approval")).json()
        approval = await client.post(
            url + "/approval",
            json={
                "approval_id": proposal["id"],
                "call_id": proposal["call"]["id"],
                "argument_digest": proposal["argument_digest"],
                "policy_version": proposal["policy_version"],
                "expires_at": proposal["expires_at"],
                "decision": "approve",
            },
        )
        assert approval.status_code == 200
        async with asyncio.timeout(15):
            while True:
                result = (await client.get(url)).json()
                if result["finished_at"]:
                    break
                await asyncio.sleep(0.02)
        assert result["status"] == "SUCCEEDED" and result["usage"]["requests"] == 3


async def child_main() -> None:
    dsn, schema = (
        os.environ["TEST_RUNWEAVE_POSTGRES_URL"],
        os.environ["TEST_RUNWEAVE_POSTGRES_SCHEMA"],
    )
    engine = engine_for(dsn, schema)
    db = Database(dsn, schema, engine, PostgresRuns(engine))
    runner = CoordinatedRunner(Model(db), ToolRegistry(FixtureHealth(engine)), Telemetry())
    claim = await db.store.claim(10)
    assert claim is not None
    persistence = CrashStore(engine, claim)
    persistence.mode = sys.argv[1]
    with (
        principal_scope(LOCAL_PRINCIPAL),
        budget_scope(db.store.ledger(claim)),
        checkpoint_scope(persistence),
    ):
        await runner.run(INCIDENT)
    raise AssertionError("Expected worker termination")


if __name__ == "__main__":
    asyncio.run(child_main())
