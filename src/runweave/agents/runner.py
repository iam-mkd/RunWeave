from runweave.agents.contracts import AgentModel, IncidentRequest, IncidentResult
from runweave.agents.triage import TriageState, TriageSteps, validate_triage_result
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
    ) -> None:
        self._steps = TriageSteps(
            model,
            tools if tools is not None else ToolRegistry(health_tool, runbook_delay=runbook_delay),
            telemetry or Telemetry(),
            step_timeout,
        )
        self._max_steps = max_steps
        self._completion_budget = completion_budget

    async def run(self, incident: IncidentRequest) -> IncidentResult:
        state = TriageState(incident, self._max_steps, self._completion_budget)
        while True:
            action = await self._steps.decide(state)
            if isinstance(action, IncidentResult):
                return validate_triage_result(state, action)
            await self._steps.collect(state, action)
