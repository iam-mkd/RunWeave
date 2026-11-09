from runweave.agents.contracts import (
    IncidentRequest,
    IncidentResult,
    ModelDecision,
    ToolCall,
    ToolExchange,
)
from runweave.tools.contracts import HealthObservation, HealthQuery
from runweave.tools.registry import HEALTH, RUNBOOK


class FakeAgentModel:
    async def decide(
        self,
        incident: IncidentRequest,
        history: tuple[ToolExchange, ...],
        tools: tuple[dict[str, object], ...],
        completion_budget: int,
    ) -> ModelDecision:
        if len(history) < 2:
            call = ToolCall(
                id=f"mock-{len(history)}",
                name=(HEALTH, RUNBOOK)[len(history)],
                arguments=HealthQuery(service=incident.service).model_dump_json(),
            )
            return ModelDecision(call, completion_tokens=1)

        observation = HealthObservation.model_validate_json(history[0].evidence.content)
        result = IncidentResult(
            summary=(
                f"Synthetic triage for {incident.service}: {incident.description}. "
                f"Mock service health is {observation.status}. {history[1].evidence.content}"
            ),
            severity="medium" if observation.status == "degraded" else "low",
            evidence_ids=tuple(exchange.evidence.id for exchange in history),
        )
        return ModelDecision(result, completion_tokens=1)
