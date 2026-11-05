from typing import Annotated, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

ServiceName = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9-]{0,62}$")]


class HealthQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    service: ServiceName


class HealthObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    service: ServiceName
    status: Literal["healthy", "degraded"]
    evidence_id: str = Field(min_length=1, max_length=100)


class HealthTool(Protocol):
    async def execute(self, query: HealthQuery) -> HealthObservation: ...
