from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from runweave.agents.contracts import IncidentResult


class RunStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"
    CANCELLED = "CANCELLED"

    @property
    def terminal(self) -> bool:
        return self not in (RunStatus.QUEUED, RunStatus.RUNNING)


class RunError(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: str
    message: str


class RunSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    status: RunStatus
    status_url: str
    submitted_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    result: IncidentResult | None = None
    error: RunError | None = None
