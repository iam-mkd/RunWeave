from typing import Annotated, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from runweave.tools.contracts import HealthObservation, HealthQuery, ServiceName


class IncidentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    service: ServiceName
    description: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)
    ]


class IncidentResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    summary: str = Field(min_length=1, max_length=2500)
    severity: Literal["low", "medium", "high"]
    evidence_ids: tuple[str, ...] = Field(min_length=1, max_length=10)


class AgentModel(Protocol):
    async def plan(self, incident: IncidentRequest) -> HealthQuery: ...

    async def summarize(
        self, incident: IncidentRequest, observation: HealthObservation
    ) -> IncidentResult: ...


class AgentExecutor(Protocol):
    async def run(self, incident: IncidentRequest) -> IncidentResult: ...
