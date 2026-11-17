import asyncio
import json

import pytest
from pydantic import ValidationError
from test_checkpoints import INCIDENT, CountingHealth, MemoryCheckpoints

from runweave.agents.context import estimated_tokens, model_payload
from runweave.agents.contracts import (
    Evidence,
    IncidentRequest,
    ModelDecision,
    ToolCall,
    ToolExchange,
)
from runweave.agents.coordination import CoordinatedRunner, CoordinationError
from runweave.agents.fake import FakeAgentModel
from runweave.agents.handoffs import HandoffRequest, current_role, role_scope
from runweave.budgets import BudgetExceeded, BudgetPolicy, BudgetState, MemoryBudget, budget_scope
from runweave.checkpoints import checkpoint_scope, decode_checkpoint, initial_checkpoint
from runweave.deadline import RUN_DEADLINE, DeadlineExceeded, deadline_scope
from runweave.security import LOCAL_PRINCIPAL, PolicyDenied, current_principal, principal_scope
from runweave.telemetry import Telemetry
from runweave.tools.contracts import HealthObservation
from runweave.tools.registry import HEALTH, RUNBOOK, ToolRegistry


class Tools(ToolRegistry):
    def __init__(self, *, barrier: bool = False, fail: bool = False) -> None:
        super().__init__(CountingHealth())
        self.barrier, self.fail = barrier, fail
        self.calls: list[str] = []
        self.scopes: list[frozenset[str]] = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled: list[str] = []

    async def execute(self, call: ToolCall, incident: IncidentRequest) -> Evidence:
        self.validate(call, incident)
        self.calls.append(call.name)
        self.scopes.append(current_principal().scopes)
        if len(self.calls) == 2:
            self.started.set()
        try:
            if self.barrier:
                await self.started.wait()
                await self.release.wait()
            else:
                await asyncio.sleep(0)
            if self.fail and call.name == RUNBOOK:
                raise RuntimeError("Synthetic read failure")
            if call.name == HEALTH:
                return Evidence(
                    "health:1",
                    HealthObservation(
                        service=incident.service, status="healthy", evidence_id="health:1"
                    ).model_dump_json(),
                )
            return Evidence("book:1", "Synthetic runbook")
        except asyncio.CancelledError:
            self.cancelled.append(call.name)
            raise


class Model(FakeAgentModel):
    def __init__(self, attack: str = "") -> None:
        self.roles: list[str | None] = []
        self.attack = attack

    async def decide(
        self,
        incident: IncidentRequest,
        history: tuple[ToolExchange, ...],
        tools: tuple[dict[str, object], ...],
        completion_budget: int,
    ) -> ModelDecision:
        self.roles.append(current_role())
        if (self.attack == "branch" and not history) or (self.attack == "merge" and history):
            return ModelDecision(
                ToolCall(id="attack", name="delegate", arguments='{"agent":"shell"}'), 1
            )
        return await super().decide(incident, history, tools, completion_budget)


def checkpoints() -> MemoryCheckpoints:
    store = MemoryCheckpoints()
    store.saved = initial_checkpoint(INCIDENT, "coordinated", 7, BudgetPolicy())
    return store


async def test_two_specialists_overlap_and_merge_scoped_evidence() -> None:
    tools, model, saved = Tools(barrier=True), Model(), checkpoints()
    runner = CoordinatedRunner(model, tools, Telemetry())
    with budget_scope(saved.ledger), checkpoint_scope(saved):
        task = asyncio.create_task(runner.run(INCIDENT))
        async with asyncio.timeout(2):
            await tools.started.wait()
        tools.release.set()
        result = await task
    assert result.evidence_ids == ("health:1", "book:1") and result.severity == "low"
    assert set(model.roles[:2]) == {"health", "runbook"} and model.roles[-1] == "coordinator"
    assert set(tools.scopes) == {frozenset({f"tools:{HEALTH}"}), frozenset({f"tools:{RUNBOOK}"})}
    assert saved.ledger.state.requests == 3
    assert saved.saved.next_node == "done" and saved.saved.steps_remaining == 2
    assert all(branch.stage == "succeeded" for branch in saved.saved.branches.values())
    with budget_scope(saved.ledger), checkpoint_scope(saved):
        assert await runner.run(INCIDENT) == result
    assert saved.ledger.state.requests == 3 and len(tools.calls) == 2


async def test_partial_failure_retains_success_without_merge_or_retry() -> None:
    tools, model, saved = Tools(fail=True), Model(), checkpoints()
    runner = CoordinatedRunner(model, tools, Telemetry())
    for _ in range(2):
        with budget_scope(saved.ledger), checkpoint_scope(saved), pytest.raises(CoordinationError):
            await runner.run(INCIDENT)
    assert saved.saved.branches["health"].stage == "succeeded"
    assert saved.saved.branches["runbook"].stage == "failed"
    assert len(model.roles) == len(tools.calls) == 2


async def test_cancellation_cleans_up_both_specialists() -> None:
    tools, model = Tools(barrier=True), Model()
    task = asyncio.create_task(CoordinatedRunner(model, tools, Telemetry()).run(INCIDENT))
    async with asyncio.timeout(2):
        await tools.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert set(tools.cancelled) == {HEALTH, RUNBOOK}
    assert not [t for t in asyncio.all_tasks() if t.get_name().startswith("specialist:")]


async def test_parent_deadline_cancels_specialists() -> None:
    tools = Tools(barrier=True)
    with pytest.raises(DeadlineExceeded) as error:
        async with deadline_scope(asyncio.get_running_loop().time() + 0.05, RUN_DEADLINE):
            await CoordinatedRunner(Model(), tools, Telemetry()).run(INCIDENT)
    assert error.value.code == RUN_DEADLINE
    assert set(tools.cancelled) == {HEALTH, RUNBOOK}


@pytest.mark.parametrize(
    "policy", [BudgetPolicy(max_requests=2), BudgetPolicy(max_output_tokens=2)]
)
async def test_insufficient_parent_budget_prevents_handoffs(policy: BudgetPolicy) -> None:
    tools, model = Tools(), Model()
    with budget_scope(MemoryBudget(BudgetState(policy=policy))), pytest.raises(BudgetExceeded):
        await CoordinatedRunner(model, tools, Telemetry(), budget_policy=policy).run(INCIDENT)
    assert tools.calls == model.roles == []


async def test_small_output_budget_uses_sequential_fallback() -> None:
    policy = BudgetPolicy(max_output_tokens=3)
    telemetry, ledger = Telemetry(), MemoryBudget(BudgetState(policy=policy))
    with budget_scope(ledger):
        result = await CoordinatedRunner(
            Model(), Tools(), telemetry, budget_policy=policy, completion_budget=3
        ).run(INCIDENT)
    assert result.severity == "low" and ledger.state.output_tokens == 3
    assert telemetry.audit_decisions[("coordination", "sequential")] == 1


@pytest.mark.parametrize("scope", ["agents:delegate", "tools:check_health"])
async def test_unauthorized_delegation_has_no_model_or_tool_effect(scope: str) -> None:
    model, tools = Model(), Tools()
    parent = LOCAL_PRINCIPAL.model_copy(update={"scopes": LOCAL_PRINCIPAL.scopes - {scope}})
    with principal_scope(parent), pytest.raises(PolicyDenied):
        await CoordinatedRunner(model, tools, Telemetry()).run(INCIDENT)
    assert tools.calls == model.roles == []


@pytest.mark.parametrize("attack", ["branch", "merge"])
async def test_model_cannot_expand_delegation(attack: str) -> None:
    with pytest.raises(CoordinationError):
        await CoordinatedRunner(Model(attack), Tools(), Telemetry()).run(INCIDENT)


async def test_nested_delegation_is_denied_and_request_roles_are_closed() -> None:
    with role_scope("health"), pytest.raises(CoordinationError):
        await CoordinatedRunner(Model(), Tools(), Telemetry()).run(INCIDENT)
    with pytest.raises(ValidationError):
        HandoffRequest.model_validate({"id": "1", "role": "shell", "service": "payments"})


async def test_coordinated_fixture_quality_and_regression_gate() -> None:
    pytest.importorskip("pydantic_evals")
    from runweave.evaluation.suite import evaluate

    assert (await evaluate("coordinated"))["passed"]
    assert not (await evaluate("coordinated", "severity"))["passed"]


async def test_specialists_cannot_overspend_shared_input_budget() -> None:
    tools = Tools()
    sizes = []
    for role in ("health", "runbook"):
        definitions = (tools.definitions()[0 if role == "health" else 1],)
        with role_scope(role):
            sizes.append(estimated_tokens(model_payload(INCIDENT, (), definitions, "fake-v1", 512)))
    policy = BudgetPolicy(max_input_tokens=max(sizes) + 1)
    ledger, model = MemoryBudget(BudgetState(policy=policy)), Model()
    with budget_scope(ledger), pytest.raises(CoordinationError) as error:
        await CoordinatedRunner(model, tools, Telemetry(), budget_policy=policy).run(INCIDENT)
    assert error.value.code == "RUN_BUDGET_EXCEEDED"
    assert ledger.state.requests == len(model.roles) == 1
    assert ledger.state.input_tokens <= policy.max_input_tokens and tools.calls == [HEALTH]


async def test_completed_branch_correlation_is_rechecked() -> None:
    saved = checkpoints()
    runner = CoordinatedRunner(Model(), Tools(), Telemetry())
    with budget_scope(saved.ledger), checkpoint_scope(saved):
        await runner.run(INCIDENT)
    raw = saved.saved.model_dump(mode="json")
    raw["branches"]["health"]["request"]["id"] = "foreign:health"
    raw["branches"]["health"]["output"]["request"]["id"] = "foreign:health"
    saved.saved = decode_checkpoint(raw)
    with (
        budget_scope(saved.ledger),
        checkpoint_scope(saved),
        pytest.raises(CoordinationError) as error,
    ):
        await runner.run(INCIDENT)
    assert error.value.code == "HANDOFF_IDENTITY_MISMATCH"


async def test_specialist_and_coordinator_spans_share_parent_trace(
    capsys: pytest.CaptureFixture[str],
) -> None:
    telemetry = Telemetry(console_traces=True)
    try:
        with telemetry.span("test.parent"):
            await CoordinatedRunner(Model(), Tools(), telemetry).run(INCIDENT)
    finally:
        await telemetry.close()
    rendered, decoder, spans = capsys.readouterr().out, json.JSONDecoder(), []
    while rendered.strip():
        span, end = decoder.raw_decode(rendered.lstrip())
        spans.append(span)
        rendered = rendered.lstrip()[end:]
    parent = next(span for span in spans if span["name"] == "test.parent")
    children = [
        span
        for span in spans
        if span["name"]
        in ("agent.specialist.health", "agent.specialist.runbook", "agent.coordinator")
    ]
    assert len(children) == 3
    assert all(
        span["context"]["trace_id"] == parent["context"]["trace_id"]
        and span["parent_id"] == parent["context"]["span_id"]
        for span in children
    )
