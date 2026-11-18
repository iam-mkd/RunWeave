"""One reproducible durable release demo, including a real worker-process crash."""

import asyncio
import json
import os
import sys
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from test_postgres_checkpoints import Database, engine_for, record
from test_postgres_checkpoints import database as database

import runweave.core.runtime as runtime_module
from runweave.agents.contracts import IncidentRequest, ModelDecision, ToolExchange
from runweave.agents.fake import FakeAgentModel
from runweave.app import create_app
from runweave.checkpoints import TriageCheckpoint, decode_checkpoint
from runweave.core.settings import Settings
from runweave.evaluation.shadow import ShadowReplay, ShadowSnapshot, pin
from runweave.evaluation.suite import Fixture
from runweave.memory import MemorySelection, recalled_payload
from runweave.security import LOCAL_PRINCIPAL, ContentPolicy
from runweave.storage.checkpoints import PostgresCheckpoints
from runweave.storage.memory import PostgresMemory
from runweave.storage.postgres import PostgresRuns
from runweave.tools.registry import RUNBOOK

FILE = Path(__file__).resolve()
ROOT = FILE.parents[1]
INCIDENT = IncidentRequest(service="payments", description="Synthetic release verification")


def settings(dsn: str) -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        model_provider="fake",
        auth_mode="local",
        database_url=SecretStr(dsn),
        worker_count=1,
        agent_workflow="coordinated",
        db_poll_seconds=0.05,
        lease_seconds=2,
        run_timeout_seconds=90,
        console_traces=True,
    )


async def until(client: httpx.AsyncClient, url: str, status: str) -> dict[str, object]:
    async with asyncio.timeout(20):
        while True:
            response = await client.get(url)
            response.raise_for_status()
            result: dict[str, object] = response.json()
            if result["status"] == status:
                return result
            if result.get("finished_at"):
                raise AssertionError(f"Unexpected terminal outcome: {result['status']}")
            await asyncio.sleep(0.02)


class CrashAfterBranch(PostgresCheckpoints):
    async def save(self, checkpoint: TriageCheckpoint, expected_sequence: int) -> TriageCheckpoint:
        saved = await super().save(checkpoint, expected_sequence)
        branch = saved.branches.get("health")
        if branch is not None and branch.stage == "succeeded":
            os._exit(17)
        return saved


class RecallModel(FakeAgentModel):
    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine

    async def decide(
        self,
        incident: IncidentRequest,
        history: tuple[ToolExchange, ...],
        tools: tuple[dict[str, object], ...],
        completion_budget: int,
    ) -> ModelDecision:
        for item in recalled_payload():
            await record(self.engine, "recall", str(item["id"]))
        return await super().decide(incident, history, tools, completion_budget)


async def test_final_durable_recovery_recall_approval_and_shadow_demo(
    database: Database, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config = settings(database.dsn)
    monkeypatch.setattr(
        runtime_module, "postgres_engine", lambda _: engine_for(database.dsn, database.schema)
    )
    monkeypatch.setattr(runtime_module, "FakeAgentModel", lambda: RecallModel(database.engine))
    app = create_app(config)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        submitted = await client.post("/v1/runs", json=INCIDENT.model_dump(mode="json"))
        assert submitted.status_code == 202
        source = await until(client, submitted.json()["status_url"], "SUCCEEDED")
        source_result = source["result"]
        assert isinstance(source_result, dict)
        created = await client.post(
            "/v1/memory",
            json={"source_run_id": source["id"], "evidence_id": source_result["evidence_ids"][0]},
        )
        assert created.status_code == 201
    memory = PostgresMemory(database.engine, config, ContentPolicy())
    initial_facts = await memory.recall("payments", MemorySelection(), LOCAL_PRINCIPAL)
    assert len(initial_facts) == 1
    async with database.engine.connect() as conn:
        initial_checkpoint = decode_checkpoint(
            await conn.scalar(
                text("SELECT checkpoint FROM runweave_runs WHERE id=:id"), {"id": source["id"]}
            )
        )
    runbook = next(
        item.evidence.content for item in initial_checkpoint.history if item.call.name == RUNBOOK
    )
    incident = INCIDENT.model_copy(update={"memory": MemorySelection(), "create_ticket": True})
    admitted = await database.store.admit(
        incident,
        capacity=10,
        run_seconds=90,
        attempts=3,
        history_max=100,
        workflow="coordinated",
        budget_policy=config.budget_policy(),
    )
    assert admitted is not None
    child = await asyncio.create_subprocess_exec(
        sys.executable,
        str(FILE),
        env={**os.environ, "TEST_RUNWEAVE_POSTGRES_SCHEMA": database.schema},
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(child.communicate(), 30)
        assert child.returncode == 17, out.decode() + err.decode()
    finally:
        if child.returncode is None:
            child.kill()
            await child.wait()
    async with database.engine.begin() as conn:
        raw = await conn.scalar(
            text("SELECT checkpoint FROM runweave_runs WHERE id=:id"), {"id": admitted.id}
        )
        crashed = decode_checkpoint(raw)
        assert crashed.branches["health"].stage == "succeeded"
        await conn.execute(
            text("UPDATE runweave_runs SET lease_until=now()-interval '1 second' WHERE id=:id"),
            {"id": admitted.id},
        )
    await database.store.maintain(retry_delay=0, history_ttl=3600, history_max=100)
    restarted = create_app(config)
    async with (
        restarted.router.lifespan_context(restarted),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=restarted), base_url="http://test"
        ) as client,
    ):
        waiting = await until(client, admitted.status_url, "WAITING_APPROVAL")
        async with database.engine.connect() as conn:
            assert await conn.scalar(text("SELECT count(*) FROM runweave_sandbox_tickets")) == 0
        proposal = (await client.get(admitted.status_url + "/approval")).json()
        bindings = {
            "approval_id": proposal["id"],
            "call_id": proposal["call"]["id"],
            "argument_digest": proposal["argument_digest"],
            "policy_version": proposal["policy_version"],
            "expires_at": proposal["expires_at"],
            "decision": "approve",
        }
        approved = await client.post(admitted.status_url + "/approval", json=bindings)
        assert approved.status_code == 200
        result = await until(client, admitted.status_url, "SUCCEEDED")
        repeated = await client.post(admitted.status_url + "/approval", json=bindings)
        assert repeated.status_code == 200
        metrics = (await client.get("/metrics")).text
        assert "runweave_runs_finished_total" in metrics
    async with database.engine.connect() as conn:
        saved = decode_checkpoint(
            await conn.scalar(
                text("SELECT checkpoint FROM runweave_runs WHERE id=:id"), {"id": admitted.id}
            )
        )
        tickets_before = await conn.scalar(text("SELECT count(*) FROM runweave_sandbox_tickets"))
        memory_before = await conn.scalar(text("SELECT count(*) FROM runweave_memory"))
        attempts = await conn.scalar(
            text("SELECT attempt FROM runweave_runs WHERE id=:id"), {"id": admitted.id}
        )
        recall_events = await conn.scalar(
            text("SELECT count(*) FROM fixture_events WHERE kind='recall' AND operation_id=:id"),
            {"id": str(initial_facts[0].id)},
        )
    assert tickets_before == 1 and attempts >= 3
    assert recall_events > 0
    assert saved.branches["health"].output == crashed.branches["health"].output
    assert result["usage"] == waiting["usage"]
    snapshot = ShadowSnapshot(
        fixture=Fixture(name="durable-release", incident=incident, runbook=runbook),
        facts=initial_facts,
    )
    async with ShadowReplay(pin("coordinated"), pin("baseline")) as replay:
        pair = await replay.compare(snapshot)
    assert (
        pair.primary.result is not None
        and pair.primary.result.model_dump(mode="json") == result["result"]
    )
    assert pair.user_result == pair.candidate.result and all(pair.candidate.quality.values())
    assert pair.primary.simulated_effects == pair.candidate.simulated_effects == 1
    async with database.engine.connect() as conn:
        assert (
            await conn.scalar(text("SELECT count(*) FROM runweave_sandbox_tickets"))
            == tickets_before
        )
        assert await conn.scalar(text("SELECT count(*) FROM runweave_memory")) == memory_before
    spans, decoder, rendered = [], json.JSONDecoder(), capsys.readouterr().out
    while rendered.strip():
        span, end = decoder.raw_decode(rendered.lstrip())
        spans.append(span)
        rendered = rendered.lstrip()[end:]
    names = {span["name"] for span in spans}
    assert {"agent.run", "agent.specialist.runbook", "agent.coordinator"} <= names
    report = {
        "schema_version": "release-demo-v1",
        "passed": True,
        "model": "fake-v1",
        "worker_crash_exit_code": child.returncode,
        "attempts": attempts,
        "committed_health_branch_reused": True,
        "private_recall_fact_count": len(initial_facts),
        "primary_model_recall_events": recall_events,
        "approved_ticket_count": tickets_before,
        "duplicate_approval_created_no_ticket": True,
        "memory_count_unchanged_after_shadow": memory_before,
        "trace_span_names": sorted(names),
        "trace_ids": sorted({span["context"]["trace_id"] for span in spans}),
        "primary_usage_after_resume": result["usage"],
        "shadow": pair.model_dump(mode="json"),
        "shadow_differences": pair.differences(),
        "external_effects": 0,
    }
    output = os.environ.get("TEST_RUNWEAVE_RELEASE_REPORT")
    if output:
        path = await asyncio.to_thread(Path(output).resolve)
        if not path.is_relative_to(ROOT):
            raise ValueError("Report must stay in the workspace")
        await asyncio.to_thread(path.parent.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(
            path.write_text, json.dumps(report, indent=2, sort_keys=True) + "\n", "utf-8"
        )


async def child_main() -> None:
    dsn, schema = (
        os.environ["TEST_RUNWEAVE_POSTGRES_URL"],
        os.environ["TEST_RUNWEAVE_POSTGRES_SCHEMA"],
    )
    engine = engine_for(dsn, schema)
    patcher = pytest.MonkeyPatch()
    patcher.setattr(runtime_module, "postgres_engine", lambda settings: engine_for(dsn, schema))
    patcher.setattr(runtime_module, "FakeAgentModel", lambda: RecallModel(engine))
    PostgresRuns.checkpoints = lambda self, claim: CrashAfterBranch(engine, claim)  # type: ignore[method-assign]
    app = create_app(settings(dsn))
    async with app.router.lifespan_context(app):
        await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(child_main())
