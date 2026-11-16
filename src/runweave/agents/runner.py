from runweave.agents.contracts import AgentModel, IncidentRequest, IncidentResult
from runweave.agents.triage import TriageSteps
from runweave.budgets import BudgetPolicy, run_budget
from runweave.security import ContentPolicy
from runweave.telemetry import Telemetry
from runweave.tools.contracts import HealthTool
from runweave.tools.registry import ToolRegistry


class AgentRunner:
    def __init__(
        self,
        model: AgentModel,
        health_tool: HealthTool,
        *,
        step_timeout: float = 5.0,
        max_steps: int = 7,
        completion_budget: int = 2048,
        telemetry: Telemetry | None = None,
        runbook_delay: float = 0,
        tools: ToolRegistry | None = None,
        content_policy: ContentPolicy | None = None,
        budget_policy: BudgetPolicy | None = None,
    ) -> None:
        self._steps = TriageSteps(
            model,
            tools
            if tools is not None
            else ToolRegistry(health_tool, runbook_delay=runbook_delay, telemetry=telemetry),
            telemetry or Telemetry(),
            step_timeout,
            content_policy=content_policy,
            budget_policy=budget_policy,
        )
        self._max_steps = max_steps
        self._completion_budget = completion_budget

    async def run(self, incident: IncidentRequest) -> IncidentResult:
        with run_budget(self._steps.budget_policy):
            state = await self._steps.restore(
                incident, self._max_steps, self._completion_budget, "baseline"
            )
            while True:
                action = await self._steps.decide(state)
                if isinstance(action, IncidentResult):
                    return await self._steps.finish(state, action)
                await self._steps.collect(state, action)
