from runweave.agents.contracts import IncidentRequest, IncidentResult
from runweave.tools.contracts import HealthObservation, HealthQuery


class FakeAgentModel:
    async def plan(self, incident: IncidentRequest) -> HealthQuery:
        return HealthQuery(service=incident.service)

    async def summarize(
        self, incident: IncidentRequest, observation: HealthObservation
    ) -> IncidentResult:
        return IncidentResult(
            summary=(
                f"Synthetic triage for {incident.service}: {incident.description}. "
                f"Mock service health is {observation.status}."
            ),
            severity="medium" if observation.status == "degraded" else "low",
            evidence_ids=(observation.evidence_id,),
        )
