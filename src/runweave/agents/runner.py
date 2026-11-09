import asyncio

from runweave.agents.contracts import AgentModel, IncidentRequest, IncidentResult
from runweave.deadline import STEP_DEADLINE, deadline_scope
from runweave.tools.contracts import HealthTool


class AgentRunner:
    def __init__(
        self, model: AgentModel, health_tool: HealthTool, *, step_timeout: float = 5.0
    ) -> None:
        self._model = model
        self._health_tool = health_tool
        self._step_timeout = step_timeout

    async def run(self, incident: IncidentRequest) -> IncidentResult:
        loop = asyncio.get_running_loop()
        async with deadline_scope(loop.time() + self._step_timeout, STEP_DEADLINE):
            query = await self._model.plan(incident)
        if query.service != incident.service:
            raise ValueError("Model selected a service outside the incident scope")

        async with deadline_scope(loop.time() + self._step_timeout, STEP_DEADLINE):
            observation = await self._health_tool.execute(query)
        if observation.service != query.service:
            raise ValueError("Tool observation does not match the requested service")

        async with deadline_scope(loop.time() + self._step_timeout, STEP_DEADLINE):
            return await self._model.summarize(incident, observation)
