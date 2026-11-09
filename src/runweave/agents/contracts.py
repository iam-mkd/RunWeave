from dataclasses import dataclass
from typing import Annotated, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from runweave.tools.contracts import ServiceName


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
    evidence_ids: tuple[Annotated[str, StringConstraints(min_length=1, max_length=100)], ...] = (
        Field(min_length=1, max_length=10)
    )


class ToolCall(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True)

    id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=64)
    arguments: str = Field(min_length=1, max_length=1024)


@dataclass(frozen=True)
class Evidence:
    id: str
    content: str


@dataclass(frozen=True)
class ToolExchange:
    call: ToolCall
    evidence: Evidence


@dataclass(frozen=True)
class ModelDecision:
    action: ToolCall | IncidentResult
    completion_tokens: int


class AgentModel(Protocol):
    async def decide(
        self,
        incident: IncidentRequest,
        history: tuple[ToolExchange, ...],
        tools: tuple[dict[str, object], ...],
        completion_budget: int,
    ) -> ModelDecision: ...


class AgentExecutor(Protocol):
    async def run(self, incident: IncidentRequest) -> IncidentResult: ...
