from runweave.agents.contracts import Evidence, IncidentRequest, ToolCall
from runweave.tools.contracts import HealthQuery, HealthTool

HEALTH = "check_health"
RUNBOOK = "lookup_runbook"


def tool_definition(name: str, description: str, schema: dict[str, object]) -> dict[str, object]:
    return {
        "type": "function",
        "function": {"name": name, "description": description, "parameters": schema},
    }


class ToolRegistry:
    def __init__(self, health_tool: HealthTool) -> None:
        self._health = health_tool

    def definitions(self) -> tuple[dict[str, object], ...]:
        return (
            tool_definition(
                HEALTH, "Read synthetic service health", HealthQuery.model_json_schema()
            ),
            tool_definition(
                RUNBOOK, "Read the local incident triage fixture", HealthQuery.model_json_schema()
            ),
        )

    async def execute(self, call: ToolCall, incident: IncidentRequest) -> Evidence:
        if call.name not in (HEALTH, RUNBOOK):
            raise ValueError("Unknown or unauthorized tool")
        query = HealthQuery.model_validate_json(call.arguments, strict=True)
        if query.service != incident.service:
            raise ValueError("Tool service does not match the incident scope")
        if call.name == HEALTH:
            observation = await self._health.execute(query)
            if observation.service != query.service:
                raise ValueError("Tool observation does not match the requested service")
            return Evidence(observation.evidence_id, observation.model_dump_json())
        return Evidence(
            "mock-runbook:triage-v1",
            "Check recent deployments and downstream errors. Compare latency and error rates "
            "against baseline before escalating. This is a synthetic runbook fixture.",
        )
