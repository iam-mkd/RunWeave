import asyncio
from dataclasses import dataclass, field

from runweave.agents.contracts import (
    AgentModel,
    IncidentRequest,
    IncidentResult,
    ToolCall,
    ToolExchange,
)
from runweave.deadline import STEP_DEADLINE, deadline_scope
from runweave.telemetry import Telemetry
from runweave.tools.registry import HEALTH, RUNBOOK, ToolRegistry

_BUDGET_EXHAUSTED = "Agent step or completion token budget exhausted"


@dataclass
class TriageState:
    incident: IncidentRequest
    steps_remaining: int
    tokens_remaining: int
    history: list[ToolExchange] = field(default_factory=list)


class TriageSteps:
    def __init__(
        self, model: AgentModel, tools: ToolRegistry, telemetry: Telemetry, step_timeout: float
    ) -> None:
        self._model = model
        self._tools = tools
        self._telemetry = telemetry
        self._step_timeout = step_timeout

    async def decide(self, state: TriageState) -> ToolCall | IncidentResult:
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
        state.steps_remaining -= 1
        state.tokens_remaining -= decision.completion_tokens
        return decision.action

    async def collect(self, state: TriageState, call: ToolCall) -> None:
        if any(entry.call.name == call.name or entry.call.id == call.id for entry in state.history):
            raise ValueError("Repeated tool call")
        # Reserve a step for the model to turn the collected evidence into a result.
        if state.steps_remaining < 2 or state.tokens_remaining <= 0:
            raise ValueError(_BUDGET_EXHAUSTED)
        deadline = asyncio.get_running_loop().time() + self._step_timeout
        with self._telemetry.step(tool=True):
            async with deadline_scope(deadline, STEP_DEADLINE):
                evidence = await self._tools.execute(call, state.incident)
        state.history.append(ToolExchange(call, evidence))
        state.steps_remaining -= 1


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
