from runweave.agents.contracts import AgentModel, IncidentRequest, IncidentResult
from runweave.tools.contracts import HealthTool


class AgentRunner:
    def __init__(self, model: AgentModel, health_tool: HealthTool) -> None:
        self._model = model
        self._health_tool = health_tool

    async def run(self, incident: IncidentRequest) -> IncidentResult:
        query = await self._model.plan(incident)
        if query.service != incident.service:
            raise ValueError("Model selected a service outside the incident scope")

        observation = await self._health_tool.execute(query)
        if observation.service != query.service:
            raise ValueError("Tool observation does not match the requested service")

        return await self._model.summarize(incident, observation)
