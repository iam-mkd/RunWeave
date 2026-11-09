import asyncio

from runweave.agents.contracts import AgentModel, IncidentRequest, IncidentResult, ToolExchange
from runweave.deadline import STEP_DEADLINE, deadline_scope
from runweave.tools.contracts import HealthTool
from runweave.tools.registry import HEALTH, RUNBOOK, ToolRegistry


class AgentRunner:
    def __init__(
        self,
        model: AgentModel,
        health_tool: HealthTool,
        *,
        step_timeout: float = 5.0,
        max_steps: int = 7,
        completion_budget: int = 2048,
    ) -> None:
        self._model = model
        self._registry = ToolRegistry(health_tool)
        self._step_timeout = step_timeout
        self._max_steps = max_steps
        self._completion_budget = completion_budget

    async def run(self, incident: IncidentRequest) -> IncidentResult:
        loop = asyncio.get_running_loop()
        history: list[ToolExchange] = []
        called: set[str] = set()
        remaining = self._completion_budget
        steps = 0
        while steps < self._max_steps and remaining > 0:
            async with deadline_scope(loop.time() + self._step_timeout, STEP_DEADLINE):
                decision = await self._model.decide(
                    incident, tuple(history), self._registry.definitions(), remaining
                )
            steps += 1
            if not 0 < decision.completion_tokens <= remaining:
                raise ValueError("Invalid completion token usage")
            remaining -= decision.completion_tokens
            action = decision.action
            if isinstance(action, IncidentResult):
                evidence_ids = {exchange.evidence.id for exchange in history}
                if (
                    called != {HEALTH, RUNBOOK}
                    or len(evidence_ids) != len(history)
                    or len(action.evidence_ids) != len(history)
                    or set(action.evidence_ids) != evidence_ids
                ):
                    raise ValueError(
                        "Final answer must cite the observed health and runbook evidence"
                    )
                return action
            if action.name in called or any(entry.call.id == action.id for entry in history):
                raise ValueError("Repeated tool call")
            if steps + 1 >= self._max_steps or remaining == 0:
                break
            async with deadline_scope(loop.time() + self._step_timeout, STEP_DEADLINE):
                evidence = await self._registry.execute(action, incident)
            history.append(ToolExchange(action, evidence))
            called.add(action.name)
            steps += 1
        raise ValueError("Agent step or completion token budget exhausted")
