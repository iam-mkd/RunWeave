import asyncio
from dataclasses import dataclass, field

from runweave.agents.budgeted import BudgetedModel
from runweave.agents.contracts import (
    AgentModel,
    Evidence,
    IncidentRequest,
    IncidentResult,
    ToolCall,
    ToolExchange,
)
from runweave.budgets import BudgetPolicy, current_ledger
from runweave.checkpoints import (
    CheckpointError,
    Node,
    TriageCheckpoint,
    Workflow,
    current_checkpoints,
    replayable_read,
    workflow_identity,
)
from runweave.deadline import STEP_DEADLINE, deadline_scope
from runweave.security import ContentPolicy
from runweave.telemetry import Telemetry
from runweave.tools.registry import HEALTH, RUNBOOK, ToolRegistry

_BUDGET_EXHAUSTED = "Agent step or completion token budget exhausted"


@dataclass
class TriageState:
    incident: IncidentRequest
    steps_remaining: int
    tokens_remaining: int
    history: list[ToolExchange] = field(default_factory=list)
    next_node: Node = "decide"
    action: ToolCall | IncidentResult | None = None
    checkpoint: TriageCheckpoint | None = None


class TriageSteps:
    def __init__(
        self,
        model: AgentModel,
        tools: ToolRegistry,
        telemetry: Telemetry,
        step_timeout: float,
        *,
        content_policy: ContentPolicy | None = None,
        budget_policy: BudgetPolicy | None = None,
    ) -> None:
        self.budget_policy = budget_policy or BudgetPolicy()
        self._model = BudgetedModel(model, self.budget_policy, telemetry)
        self._tools = tools
        self._telemetry = telemetry
        self._step_timeout = step_timeout
        self._content_policy = content_policy or ContentPolicy()

    async def restore(
        self, incident: IncidentRequest, max_steps: int, completion_budget: int, workflow: Workflow
    ) -> TriageState:
        store = current_checkpoints()
        if store is None:
            return TriageState(incident, max_steps, completion_budget)
        saved = await store.load()
        if saved.identity != workflow_identity(workflow, self.budget_policy.model):
            raise CheckpointError("CHECKPOINT_WORKFLOW_UNSUPPORTED")
        if saved.incident != incident:
            raise CheckpointError("CHECKPOINT_INVALID")
        budget = await current_ledger().read()
        if (
            budget.policy != saved.usage.policy
            or budget.requests < saved.usage.requests
            or not saved.usage.calls.keys() <= budget.calls.keys()
        ):
            raise CheckpointError("CHECKPOINT_BUDGET_MISMATCH")
        self._content_policy.check(saved.model_dump_json())
        return TriageState(
            incident,
            saved.steps_remaining,
            saved.tokens_remaining,
            list(saved.history),
            saved.next_node,
            saved.action,
            saved,
        )

    async def _persist(self, state: TriageState) -> None:
        previous = state.checkpoint
        if previous is None:
            return
        store = current_checkpoints()
        if store is None:
            raise CheckpointError("CHECKPOINT_CONTEXT_LOST")
        updated = previous.model_copy(
            update={
                "steps_remaining": state.steps_remaining,
                "tokens_remaining": state.tokens_remaining,
                "history": list(state.history),
                "next_node": state.next_node,
                "action": state.action,
            }
        )
        self._content_policy.check(updated.model_dump_json())
        state.checkpoint = await store.save(updated, previous.sequence)
        self._telemetry.audit("checkpoint", "committed")

    async def decide(self, state: TriageState) -> ToolCall | IncidentResult:
        self._content_policy.check(state.incident.model_dump_json())
        if state.next_node != "decide":
            if state.action is None:
                raise CheckpointError("CHECKPOINT_INVALID")
            return state.action
        if state.steps_remaining <= 0 or state.tokens_remaining <= 0:
            raise ValueError(_BUDGET_EXHAUSTED)
        deadline = asyncio.get_running_loop().time() + self._step_timeout
        with self._telemetry.step(tool=False):
            async with deadline_scope(deadline, STEP_DEADLINE):
                decision = await self._model.decide(
                    state.incident,
                    tuple(state.history),
                    self._tools.definitions(),
                    state.tokens_remaining,
                )
        if not 0 < decision.completion_tokens <= state.tokens_remaining:
            raise ValueError("Invalid completion token usage")
        self._content_policy.check(decision.action.model_dump_json())
        state.steps_remaining -= 1
        state.tokens_remaining -= decision.completion_tokens
        state.action = decision.action
        state.next_node = "collect" if isinstance(decision.action, ToolCall) else "finish"
        await self._persist(state)
        return decision.action

    async def collect(self, state: TriageState, call: ToolCall) -> None:
        if any(entry.call.name == call.name or entry.call.id == call.id for entry in state.history):
            raise ValueError("Repeated tool call")
        # Reserve a step for the model to turn the collected evidence into a result.
        if state.steps_remaining < 2 or state.tokens_remaining <= 0:
            raise ValueError(_BUDGET_EXHAUSTED)
        deadline = asyncio.get_running_loop().time() + self._step_timeout
        self._tools.validate(call, state.incident)

        async def execute() -> Evidence:
            evidence = await self._tools.execute(call, state.incident)
            self._validate_evidence(evidence)
            return evidence

        with self._telemetry.step(tool=True):
            async with deadline_scope(deadline, STEP_DEADLINE):
                evidence = await replayable_read(call, execute)
        self._validate_evidence(evidence)
        state.history.append(ToolExchange(call, evidence))
        state.steps_remaining -= 1
        state.action = None
        state.next_node = "decide"
        await self._persist(state)

    def _validate_evidence(self, evidence: Evidence) -> None:
        self._content_policy.check(evidence.id)
        self._content_policy.check(evidence.content)
        if len(evidence.content) > 16384 or len(evidence.id) > 100:
            raise ValueError("Tool evidence exceeds the size limit")

    async def finish(self, state: TriageState, result: IncidentResult) -> IncidentResult:
        validated = validate_triage_result(state, result)
        self._content_policy.check(validated.model_dump_json())
        if state.next_node != "done":
            state.action = validated
            state.next_node = "done"
            await self._persist(state)
        return validated


def validate_triage_result(state: TriageState, result: IncidentResult) -> IncidentResult:
    called = {exchange.call.name for exchange in state.history}
    evidence_ids = {exchange.evidence.id for exchange in state.history}
    if (
        called != {HEALTH, RUNBOOK}
        or len(evidence_ids) != len(state.history)
        or len(result.evidence_ids) != len(state.history)
        or set(result.evidence_ids) != evidence_ids
    ):
        raise ValueError("Final answer must cite the observed health and runbook evidence")
    return result
