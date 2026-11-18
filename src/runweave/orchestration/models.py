from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from runweave.agents.catalog import TRIAGE_AGENT
from runweave.budgets import BudgetSummary
from runweave.security import LOCAL_PRINCIPAL, Principal
from runweave.steps import StepEvent

AgentName = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9-]{0,62}$")]


class RunStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"
    CANCELLED = "CANCELLED"

    @property
    def terminal(self) -> bool:
        return self not in (RunStatus.QUEUED, RunStatus.RUNNING, RunStatus.WAITING_APPROVAL)


class RunError(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: str
    message: str


class AgentRunRequest(BaseModel):
    """Submit input for any registered agent. The legacy incident body maps to triage."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    agent: AgentName
    input: dict[str, Any]


class RunSummary(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    agent: str = TRIAGE_AGENT
    agent_version: str | None = None
    status: RunStatus
    status_url: str
    submitted_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: RunError | None = None
    usage: BudgetSummary | None = None


class RunSnapshot(RunSummary):
    # The owner's validated agent input; excluded from list summaries.
    input: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    steps: tuple[StepEvent, ...] = ()
    principal: Principal = Field(default=LOCAL_PRINCIPAL, exclude=True)
    approval_wait_started_at: datetime | None = None
    approval_wait_seconds: float = Field(default=0, ge=0)

    def summary(self) -> RunSummary:
        return RunSummary.model_validate(self.model_dump(include=set(RunSummary.model_fields)))


class RunPage(BaseModel):
    runs: list[RunSummary]
