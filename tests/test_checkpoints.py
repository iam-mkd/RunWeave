import asyncio
from typing import Literal
from uuid import uuid4

import pytest

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
from runweave.budgets import BudgetPolicy, BudgetState, MemoryBudget, budget_scope
from runweave.checkpoints import (
    CheckpointError,
    OperationRecord,
    TriageCheckpoint,
    Workflow,
    checkpoint_scope,
    decode_checkpoint,
    initial_checkpoint,
    operation_identity,
)
from runweave.effects import EffectConfirmation, EffectRunner
from runweave.security import LOCAL_PRINCIPAL, ContentPolicy, PolicyDenied, principal_scope
from runweave.telemetry import Telemetry
from runweave.tools.contracts import HealthObservation, HealthQuery
from runweave.tools.registry import ToolRegistry

INCIDENT = IncidentRequest(service="payments", description="Synthetic restart")
CALL = ToolCall(id="fixture-effect", name="fixture", arguments='{"service":"payments"}')
RECEIPT = Evidence("effect:1", "Synthetic receipt")


class SimulatedCrash(RuntimeError):
    pass


class MemoryCheckpoints:
    def __init__(self, workflow: Workflow = "baseline", *, crash_at: int | None = None) -> None:
        self.run_id = uuid4()
        self.saved = initial_checkpoint(INCIDENT, workflow, 7, BudgetPolicy())
        self.ledger = MemoryBudget(BudgetState(policy=BudgetPolicy()))
        self.operations: dict[str, OperationRecord] = {}
        self.crash_at = crash_at

    async def load(self) -> TriageCheckpoint:
        return decode_checkpoint(self.saved.model_dump(mode="json"))

    async def save(self, checkpoint: TriageCheckpoint, expected_sequence: int) -> TriageCheckpoint:
        if self.saved.sequence != expected_sequence:
            raise CheckpointError("CHECKPOINT_CONFLICT")
        self.saved = decode_checkpoint(
            checkpoint.model_copy(
                update={
                    "sequence": expected_sequence + 1,
                    "usage": await self.ledger.read(),
                }
            ).model_dump(mode="json")
        )
        if self.saved.sequence == self.crash_at:
            self.crash_at = None
            raise SimulatedCrash()
        return self.saved.model_copy(deep=True)

    async def prepare_operation(
        self, operation_id: str, digest: str, kind: Literal["read", "effect"]
    ) -> tuple[OperationRecord, bool]:
        previous = self.operations.get(operation_id)
        if previous is not None:
            if previous.digest != digest or previous.kind != kind:
                raise CheckpointError("OPERATION_IDENTITY_CONFLICT")
            return previous, False
        record = OperationRecord(id=operation_id, digest=digest, kind=kind)
        self.operations[operation_id] = record
        return record, True

    async def commit_operation(self, operation_id: str, evidence: Evidence) -> None:
        self.operations[operation_id] = self.operations[operation_id].model_copy(
            update={"status": "committed", "evidence": evidence}
        )

    async def require_resolution(self, operation_id: str) -> None:
        self.operations[operation_id] = self.operations[operation_id].model_copy(
            update={"status": "needs_resolution"}
        )


class CountingHealth:
    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, query: HealthQuery) -> HealthObservation:
        self.calls += 1
        return HealthObservation(service=query.service, status="healthy", evidence_id="health:1")


class CountingModel(FakeAgentModel):
    def __init__(self) -> None:
        self.calls = 0

    async def decide(
        self,
        incident: IncidentRequest,
        history: tuple[ToolExchange, ...],
        tools: tuple[dict[str, object], ...],
        completion_budget: int,
    ) -> ModelDecision:
        self.calls += 1
        return await super().decide(incident, history, tools, completion_budget)


def runner(
    workflow: Workflow,
    model: CountingModel,
    health: CountingHealth,
    max_steps: int = 7,
    completion_budget: int = 2048,
) -> AgentExecutor:
    if workflow == "baseline":
        return AgentRunner(model, health, max_steps=max_steps, completion_budget=completion_budget)
    pytest.importorskip("pydantic_ai")
    from runweave.agents.workflow import PydanticDecisionModel, PydanticWorkflow

    return PydanticWorkflow(
        TriageSteps(PydanticDecisionModel(model), ToolRegistry(health), Telemetry(), 5),
        max_steps,
        completion_budget,
    )


@pytest.mark.parametrize("workflow", ["baseline", "pydanticai"])
@pytest.mark.parametrize("boundary", [2, 3, 6])
async def test_resume_committed_nodes_and_pinned_limits(workflow: Workflow, boundary: int) -> None:
    store = MemoryCheckpoints(workflow, crash_at=boundary)
    model, health = CountingModel(), CountingHealth()
    with checkpoint_scope(store), budget_scope(store.ledger):
        with pytest.raises(SimulatedCrash):
            await runner(workflow, model, health).run(INCIDENT)
        assert store.saved.sequence == boundary
        result = await runner(workflow, model, health, max_steps=1, completion_budget=1).run(
            INCIDENT
        )
    assert result.severity == "low"
    assert model.calls == 3
    assert health.calls == 1
    assert store.saved.sequence == 6
    assert store.saved.next_node == "done"
    assert store.saved.usage.requests == store.ledger.state.requests == 3
    assert len(store.saved.history) == 2


async def test_unsupported_checkpoint_and_identity_fail_before_inference() -> None:
    store = MemoryCheckpoints()
    model, health = CountingModel(), CountingHealth()
    original = store.saved
    bad = original.model_copy(update={"version": "triage-checkpoint-v99"})
    store.saved = bad
    with (
        checkpoint_scope(store),
        budget_scope(store.ledger),
        pytest.raises(CheckpointError) as error,
    ):
        await runner("baseline", model, health).run(INCIDENT)
    assert error.value.code == "CHECKPOINT_VERSION_UNSUPPORTED"
    store.saved = original.model_copy(
        update={"identity": original.identity.model_copy(update={"prompt_sha256": "0" * 64})}
    )
    with (
        checkpoint_scope(store),
        budget_scope(store.ledger),
        pytest.raises(CheckpointError) as error,
    ):
        await runner("baseline", model, health).run(INCIDENT)
    assert error.value.code == "CHECKPOINT_WORKFLOW_UNSUPPORTED"
    assert model.calls == health.calls == store.ledger.state.requests == 0


def test_checkpoint_rejects_inconsistent_boundary() -> None:
    raw = initial_checkpoint(INCIDENT, "baseline", 7, BudgetPolicy()).model_dump(mode="json")
    raw["next_node"] = "collect"
    with pytest.raises(CheckpointError):
        decode_checkpoint(raw)


def test_operation_key_stability_and_argument_binding() -> None:
    run_id = uuid4()
    first = operation_identity(run_id, "effect", "fixture", "call", '{"a":1,"b":2}')
    assert first == operation_identity(run_id, "effect", "fixture", "call", '{"b": 2, "a": 1}')
    changed = operation_identity(run_id, "effect", "fixture", "call", '{"a":9,"b":2}')
    assert first[0] == changed[0]
    assert first[1] != changed[1]
    assert first[0] != operation_identity(uuid4(), "effect", "fixture", "call", '{"a":1}')[0]


class Adapter:
    def __init__(
        self,
        *,
        idempotent: bool = True,
        confirmation: Literal["committed", "absent", "unknown"] = "absent",
        timeout: bool = False,
    ) -> None:
        self.idempotent = idempotent
        self.confirmation = confirmation
        self.timeout = timeout
        self.calls: list[str] = []
        self.lookups = 0

    async def perform(self, operation_id: str) -> Evidence:
        self.calls.append(operation_id)
        if self.timeout:
            raise TimeoutError("Synthetic ambiguous effect")
        return RECEIPT

    async def reconcile(self, operation_id: str) -> EffectConfirmation:
        self.lookups += 1
        return EffectConfirmation(
            status=self.confirmation, evidence=RECEIPT if self.confirmation == "committed" else None
        )


async def test_committed_effect_is_reused_and_changed_arguments_are_rejected() -> None:
    store = MemoryCheckpoints()
    adapter = Adapter()
    effects = EffectRunner(store)
    assert await effects.run(CALL, adapter) == RECEIPT
    assert await effects.run(CALL, adapter) == RECEIPT
    assert len(adapter.calls) == 1
    changed = CALL.model_copy(update={"arguments": '{"service":"other"}'})
    with pytest.raises(CheckpointError) as error:
        await effects.run(changed, adapter)
    assert error.value.code == "OPERATION_IDENTITY_CONFLICT"


@pytest.mark.parametrize(
    "idempotent,confirmation,succeeds",
    [
        (True, "absent", True),
        (False, "absent", False),
        (True, "unknown", False),
        (False, "committed", True),
    ],
)
async def test_ambiguous_effect_requires_confirmation_or_real_idempotency(
    idempotent: bool, confirmation: Literal["committed", "absent", "unknown"], succeeds: bool
) -> None:
    store = MemoryCheckpoints()
    key, digest = operation_identity(store.run_id, "effect", CALL.name, CALL.id, CALL.arguments)
    await store.prepare_operation(key, digest, "effect")
    adapter = Adapter(idempotent=idempotent, confirmation=confirmation)
    if succeeds:
        assert await EffectRunner(store).run(CALL, adapter) == RECEIPT
    else:
        with pytest.raises(CheckpointError) as error:
            await EffectRunner(store).run(CALL, adapter)
        assert error.value.code == "EFFECT_RECONCILIATION_REQUIRED"
        assert store.operations[key].status == "needs_resolution"
    assert len(adapter.calls) == int(succeeds and confirmation == "absent")


async def test_timeout_can_reconcile_a_committed_effect() -> None:
    adapter = Adapter(timeout=True, confirmation="committed")
    assert await EffectRunner(MemoryCheckpoints()).run(CALL, adapter) == RECEIPT
    assert len(adapter.calls) == adapter.lookups == 1


async def test_effect_cancellation_keeps_intent_and_propagates() -> None:
    started = asyncio.Event()

    class Waiting(Adapter):
        async def perform(self, operation_id: str) -> Evidence:
            started.set()
            await asyncio.Event().wait()
            raise AssertionError("Unreachable")

    store = MemoryCheckpoints()
    task = asyncio.create_task(EffectRunner(store).run(CALL, Waiting()))
    await asyncio.wait_for(started.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert next(iter(store.operations.values())).status == "started"


@pytest.mark.parametrize("source", ["history", "receipt"])
async def test_replay_checks_stored_data_against_current_credential_policy(source: str) -> None:
    canary = "RW_PRIVATE_CANARY_73f09"
    boundary = 2 if source == "history" else 1
    store = MemoryCheckpoints(crash_at=boundary)
    model, health = CountingModel(), CountingHealth()
    secret = Evidence(
        canary, f'{{"service":"payments","status":"healthy","evidence_id":"{canary}"}}'
    )
    with checkpoint_scope(store), budget_scope(store.ledger):
        with pytest.raises(SimulatedCrash):
            await runner("baseline", model, health).run(INCIDENT)
        if source == "history":
            previous = store.saved.history[0]
            store.saved.history[0] = ToolExchange(previous.call, secret)
        else:
            call = store.saved.action
            assert isinstance(call, ToolCall)
            key, digest = operation_identity(
                store.run_id, "read", call.name, call.id, call.arguments
            )
            await store.prepare_operation(key, digest, "read")
            await store.commit_operation(key, secret)
        with pytest.raises(PolicyDenied):
            await AgentRunner(model, health, content_policy=ContentPolicy((canary,))).run(INCIDENT)
    assert model.calls == 1
    assert health.calls == int(source == "history")


async def test_receipt_reuse_rechecks_tool_permission() -> None:
    store = MemoryCheckpoints(crash_at=1)
    model, health = CountingModel(), CountingHealth()
    with checkpoint_scope(store), budget_scope(store.ledger):
        with pytest.raises(SimulatedCrash):
            await runner("baseline", model, health).run(INCIDENT)
        call = store.saved.action
        assert isinstance(call, ToolCall)
        key, digest = operation_identity(store.run_id, "read", call.name, call.id, call.arguments)
        await store.prepare_operation(key, digest, "read")
        await store.commit_operation(key, Evidence("health:1", "cached"))
        restricted = LOCAL_PRINCIPAL.model_copy(
            update={
                "scopes": LOCAL_PRINCIPAL.scopes - {"tools:check_health"},
            }
        )
        with principal_scope(restricted), pytest.raises(PolicyDenied):
            await runner("baseline", model, health).run(INCIDENT)
    assert model.calls == 1
    assert health.calls == 0
