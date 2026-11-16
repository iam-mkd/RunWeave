import asyncio
import os
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import cast
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from runweave.agents.contracts import (
    AgentExecutor,
    Evidence,
    IncidentRequest,
    ModelDecision,
    ToolCall,
    ToolExchange,
)
from runweave.agents.fake import FakeAgentModel
from runweave.agents.runner import AgentRunner
from runweave.agents.triage import TriageSteps
from runweave.app import create_app
from runweave.budgets import budget_scope
from runweave.checkpoints import (
    CheckpointError,
    TriageCheckpoint,
    Workflow,
    checkpoint_scope,
    current_checkpoints,
)
from runweave.core.settings import Settings
from runweave.effects import EffectConfirmation, EffectRunner
from runweave.orchestration.models import RunSnapshot
from runweave.security import principal_scope
from runweave.storage.checkpoints import PostgresCheckpoints
from runweave.storage.postgres import Claim, PostgresRuns
from runweave.telemetry import Telemetry
from runweave.tools.contracts import HealthObservation, HealthQuery
from runweave.tools.registry import RUNBOOK, ToolRegistry

_FILE = Path(__file__).resolve()
_DSN = "TEST_RUNWEAVE_POSTGRES_URL"
_SCHEMA = "TEST_RUNWEAVE_POSTGRES_SCHEMA"
INCIDENT = IncidentRequest(service="payments", description="Synthetic checkpoint recovery")


def engine_for(dsn: str, schema: str) -> AsyncEngine:
    return create_async_engine(
        dsn, hide_parameters=True, connect_args={"server_settings": {"search_path": schema}}
    )


@dataclass
class Database:
    dsn: str = field(repr=False)
    schema: str
    engine: AsyncEngine
    store: PostgresRuns


@pytest.fixture
async def database() -> AsyncIterator[Database]:
    dsn = os.environ.get(_DSN)
    if not dsn:
        pytest.skip(f"Set {_DSN} to run checkpoint recovery integration tests")
    schema = f"checkpoint_test_{uuid4().hex}"
    admin = create_async_engine(dsn, hide_parameters=True)
    engine = engine_for(dsn, schema)
    async with admin.begin() as conn:
        await conn.execute(text(f"CREATE SCHEMA {schema}"))
    try:
        paths = await asyncio.to_thread(
            lambda: sorted((_FILE.parents[1] / "migrations").glob("*.sql"))
        )
        async with engine.begin() as conn:
            for path in paths:
                sql = await asyncio.to_thread(path.read_text, "utf-8")
                for statement in sql.split(";"):
                    if statement.strip():
                        await conn.execute(text(statement))
            await conn.execute(
                text("""
                CREATE TABLE fixture_events (kind text NOT NULL, operation_id text)
            """)
            )
            await conn.execute(
                text("""
                CREATE TABLE fixture_receipts (operation_id text PRIMARY KEY)
            """)
            )
        yield Database(dsn, schema, engine, PostgresRuns(engine))
    finally:
        await engine.dispose()
        async with admin.begin() as conn:
            await conn.execute(text(f"DROP SCHEMA {schema} CASCADE"))
        await admin.dispose()


async def record(engine: AsyncEngine, kind: str, operation_id: str | None = None) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO fixture_events VALUES (:kind, :operation)"),
            {"kind": kind, "operation": operation_id},
        )


class FixtureModel(FakeAgentModel):
    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine

    async def decide(
        self,
        incident: IncidentRequest,
        history: tuple[ToolExchange, ...],
        tools: tuple[dict[str, object], ...],
        completion_budget: int,
    ) -> ModelDecision:
        await record(self.engine, "model", str(len(history)))
        return await super().decide(incident, history, tools, completion_budget)


class FixtureHealth:
    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine

    async def execute(self, query: HealthQuery) -> HealthObservation:
        await record(self.engine, "health")
        return HealthObservation(
            service=query.service, status="healthy", evidence_id="fixture-health:1"
        )


def receipt(operation_id: str) -> Evidence:
    return Evidence(f"fixture-effect:{operation_id[-16:]}", "Confirmed synthetic receipt")


class FixtureEffect:
    def __init__(self, engine: AsyncEngine, mode: str, idempotent: bool) -> None:
        self.engine = engine
        self.mode = mode
        self.idempotent = idempotent

    async def perform(self, operation_id: str) -> Evidence:
        await record(self.engine, "effect_perform", operation_id)
        if self.mode == "before_effect":
            os._exit(17)
        async with self.engine.begin() as conn:
            await conn.execute(
                text("INSERT INTO fixture_receipts VALUES (:operation) ON CONFLICT DO NOTHING"),
                {"operation": operation_id},
            )
        if self.mode == "after_external":
            os._exit(17)
        return receipt(operation_id)

    async def reconcile(self, operation_id: str) -> EffectConfirmation:
        await record(self.engine, "effect_reconcile", operation_id)
        async with self.engine.connect() as conn:
            found = await conn.scalar(
                text("SELECT operation_id FROM fixture_receipts WHERE operation_id = :operation"),
                {"operation": operation_id},
            )
        return EffectConfirmation(
            status="committed" if found else "absent",
            evidence=receipt(operation_id) if found else None,
        )


class FixtureTools(ToolRegistry):
    def __init__(self, engine: AsyncEngine, mode: str = "", *, idempotent: bool = True) -> None:
        super().__init__(FixtureHealth(engine))
        self.engine = engine
        self.mode = mode
        self.idempotent = idempotent

    async def execute(self, call: ToolCall, incident: IncidentRequest) -> Evidence:
        evidence = await super().execute(call, incident)
        if call.name == RUNBOOK:
            await record(self.engine, "runbook")
            store = current_checkpoints()
            assert store is not None
            effect = ToolCall(id="fixture-ticket", name="fixture_effect", arguments=call.arguments)
            await EffectRunner(store).run(
                effect, FixtureEffect(self.engine, self.mode, self.idempotent)
            )
        return evidence


def executor(
    engine: AsyncEngine,
    workflow: Workflow,
    mode: str = "",
    *,
    idempotent: bool = True,
    max_steps: int = 7,
    completion_budget: int = 2048,
) -> AgentExecutor:
    model = FixtureModel(engine)
    tools = FixtureTools(engine, mode, idempotent=idempotent)
    if workflow == "baseline":
        return AgentRunner(
            model,
            FixtureHealth(engine),
            tools=tools,
            max_steps=max_steps,
            completion_budget=completion_budget,
        )
    from runweave.agents.workflow import PydanticDecisionModel, PydanticWorkflow

    return PydanticWorkflow(
        TriageSteps(PydanticDecisionModel(model), tools, Telemetry(), 5),
        max_steps,
        completion_budget,
    )


class CrashCheckpoints(PostgresCheckpoints):
    def __init__(self, engine: AsyncEngine, claim: Claim, mode: str) -> None:
        super().__init__(engine, claim)
        self.mode = mode
        self.engine = engine

    async def save(self, checkpoint: TriageCheckpoint, expected_sequence: int) -> TriageCheckpoint:
        if self.mode == "before_checkpoint" and expected_sequence == 0:
            os._exit(17)
        saved = await super().save(checkpoint, expected_sequence)
        boundaries = {
            "after_model": 1,
            "after_health": 2,
            "after_effect_checkpoint": 4,
            "after_done": 6,
        }
        if saved.sequence == boundaries.get(self.mode):
            os._exit(17)
        return saved

    async def commit_operation(self, operation_id: str, evidence: Evidence) -> None:
        await super().commit_operation(operation_id, evidence)
        async with self.engine.connect() as conn:
            kind = await conn.scalar(
                text("SELECT kind FROM runweave_operations WHERE operation_id = :operation"),
                {"operation": operation_id},
            )
        if (self.mode == "after_receipt" and kind == "read") or (
            self.mode == "after_effect_receipt" and kind == "effect"
        ):
            os._exit(17)


async def crash_worker(mode: str, workflow: Workflow) -> None:
    engine = engine_for(os.environ[_DSN], os.environ[_SCHEMA])
    run = executor(engine, workflow, mode)
    store = PostgresRuns(engine)
    claim = await store.claim(10)
    assert claim is not None
    with (
        principal_scope(claim.snapshot.principal),
        budget_scope(store.ledger(claim)),
        checkpoint_scope(CrashCheckpoints(engine, claim, mode)),
    ):
        await run.run(INCIDENT)
    raise AssertionError("The fixture worker did not reach its crash boundary")


async def kill_and_expire(database: Database, mode: str, workflow: Workflow) -> None:
    child = await asyncio.create_subprocess_exec(
        sys.executable,
        str(_FILE),
        "child",
        mode,
        workflow,
        env={
            **os.environ,
            _DSN: database.dsn,
            _SCHEMA: database.schema,
            "PYDANTIC_AI_NO_BANNER": "1",
        },
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(child.communicate(), 30)
        assert child.returncode == 17, stderr.decode(errors="replace")
    finally:
        if child.returncode is None:
            child.kill()
            await child.wait()
    async with database.engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE runweave_runs SET lease_until = clock_timestamp() "
                "- interval '1 second' WHERE status = 'RUNNING'"
            )
        )
    await database.store.maintain(retry_delay=0, history_ttl=60, history_max=100)


async def admit(database: Database, workflow: Workflow = "baseline") -> RunSnapshot:
    result = await database.store.admit(
        INCIDENT, capacity=10, run_seconds=90, attempts=3, history_max=100, workflow=workflow
    )
    assert result is not None
    return result


@pytest.mark.parametrize("workflow", ["baseline", "pydanticai"])
@pytest.mark.parametrize(
    "mode",
    [
        "before_checkpoint",
        "after_model",
        "after_health",
        "after_receipt",
        "before_effect",
        "after_external",
        "after_effect_receipt",
        "after_effect_checkpoint",
        "after_done",
    ],
)
async def test_worker_kill_resumes_next_committed_node(
    database: Database,
    workflow: Workflow,
    mode: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if workflow == "pydanticai":
        pytest.importorskip("pydantic_ai")
    submitted = await admit(database, workflow)
    await kill_and_expire(database, mode, workflow)
    async with database.engine.connect() as conn:
        row = (
            (
                await conn.execute(
                    text("SELECT checkpoint, checkpoint_seq, budget FROM runweave_runs")
                )
            )
            .mappings()
            .one()
        )
    assert row["checkpoint"]["sequence"] == row["checkpoint_seq"]
    charged_before = row["budget"]["requests"]
    assert charged_before >= 1
    monkeypatch.setattr(
        "runweave.core.runtime.postgres_engine",
        lambda settings: engine_for(database.dsn, database.schema),
    )
    settings = Settings(
        database_url=SecretStr(database.dsn),
        model_provider="fake",
        agent_workflow=workflow,
        worker_count=1,
        lease_seconds=3,
        db_poll_seconds=0.05,
        agent_max_steps=1,
        agent_completion_tokens=1,
    )
    app = create_app(
        settings, executor=executor(database.engine, workflow, max_steps=1, completion_budget=1)
    )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        async with asyncio.timeout(15):
            while True:
                completed = (await client.get(submitted.status_url)).json()
                if completed["finished_at"] is not None:
                    break
                await asyncio.sleep(0.02)
        assert completed["status"] == "SUCCEEDED", completed
        assert completed["result"]["severity"] == "low"
        assert completed["usage"]["requests"] == (4 if mode == "before_checkpoint" else 3)
    async with database.engine.connect() as conn:
        rows = (
            await conn.execute(text("SELECT kind, count(*) FROM fixture_events GROUP BY kind"))
        ).all()
        counts = {str(row[0]): int(row[1]) for row in rows}
        assert counts["health"] == 1
        assert counts["model"] == (4 if mode == "before_checkpoint" else 3)
        assert counts["effect_perform"] == (2 if mode == "before_effect" else 1)
        assert await conn.scalar(text("SELECT count(*) FROM fixture_receipts")) == 1
        assert await conn.scalar(text("SELECT checkpoint_seq FROM runweave_runs")) == 6
        assert (
            await conn.scalar(
                text("SELECT count(*) FROM runweave_operations WHERE status != 'committed'")
            )
            == 0
        )


async def test_checkpoint_sequence_and_stale_claims_are_fenced(database: Database) -> None:
    await admit(database)
    claim = await database.store.claim(3)
    assert claim is not None
    store = database.store.checkpoints(claim)
    initial = await store.load()
    saves = await asyncio.gather(
        store.save(initial, 0), store.save(initial, 0), return_exceptions=True
    )
    assert sum(isinstance(item, TriageCheckpoint) for item in saves) == 1
    assert sum(isinstance(item, CheckpointError) for item in saves) == 1
    stale = database.store.checkpoints(replace(claim, token=uuid4()))
    for action in (stale.load(), stale.save(initial, 0)):
        with pytest.raises(CheckpointError):
            await action
    await database.store.cancel(claim.snapshot.id)
    with pytest.raises(CheckpointError):
        await store.save(await store.load(), 1)


async def test_unknown_checkpoint_version_fails_only_its_job(
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    submitted = await admit(database)
    async with database.engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE runweave_runs SET checkpoint = jsonb_set(checkpoint, "
                "'{version}', '\"triage-checkpoint-v99\"'::jsonb)"
            )
        )
    monkeypatch.setattr(
        "runweave.core.runtime.postgres_engine",
        lambda settings: engine_for(database.dsn, database.schema),
    )
    app = create_app(
        Settings(database_url=SecretStr(database.dsn), worker_count=1, db_poll_seconds=0.05)
    )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        async with asyncio.timeout(5):
            while True:
                result = (await client.get(submitted.status_url)).json()
                if result["finished_at"] is not None:
                    break
                await asyncio.sleep(0.02)
        assert result["status"] == "FAILED"
        assert result["error"]["code"] == "CHECKPOINT_VERSION_UNSUPPORTED"
        assert result["usage"]["requests"] == 0
        assert (await client.get("/health/ready")).status_code == 200
        accepted = await client.post("/v1/runs", json=INCIDENT.model_dump())
        assert accepted.status_code == 202
        async with asyncio.timeout(5):
            while True:
                healthy = (await client.get(accepted.headers["Location"])).json()
                if healthy["finished_at"] is not None:
                    break
                await asyncio.sleep(0.02)
        assert healthy["status"] == "SUCCEEDED"


async def test_ambiguous_non_idempotent_effect_requires_manual_resolution(
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    submitted = await admit(database)
    await kill_and_expire(database, "before_effect", "baseline")
    monkeypatch.setattr(
        "runweave.core.runtime.postgres_engine",
        lambda settings: engine_for(database.dsn, database.schema),
    )
    app = create_app(
        Settings(database_url=SecretStr(database.dsn), worker_count=1, db_poll_seconds=0.05),
        executor=executor(database.engine, "baseline", idempotent=False),
    )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        async with asyncio.timeout(5):
            while True:
                result = (await client.get(submitted.status_url)).json()
                if result["finished_at"] is not None:
                    break
                await asyncio.sleep(0.02)
        assert result["status"] == "FAILED"
        assert result["error"]["code"] == "EFFECT_RECONCILIATION_REQUIRED"
    async with database.engine.connect() as conn:
        assert await conn.scalar(text("SELECT count(*) FROM fixture_receipts")) == 0
        assert (
            await conn.scalar(
                text("SELECT count(*) FROM fixture_events WHERE kind = 'effect_perform'")
            )
            == 1
        )
        assert (
            await conn.scalar(
                text("SELECT count(*) FROM runweave_operations WHERE status = 'needs_resolution'")
            )
            == 1
        )


if __name__ == "__main__":
    asyncio.run(crash_worker(sys.argv[2], cast(Workflow, sys.argv[3])))
