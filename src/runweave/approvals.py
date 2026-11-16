from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Literal, Protocol
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from runweave.agents.contracts import IncidentRequest, IncidentResult, ToolCall
from runweave.checkpoints import CheckpointError
from runweave.tools.contracts import ServiceName

TICKET_POLICY = "sandbox-ticket-v1"
TICKET_CALL_ID = "sandbox-ticket-v1"


class ApprovalError(CheckpointError):
    pass


class AwaitingApproval(Exception):
    pass


class TicketArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    service: ServiceName
    title: str = Field(min_length=1, max_length=200)
    severity: Literal["low", "medium", "high"]


class ApprovalDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    approval_id: UUID
    call_id: str = Field(min_length=1, max_length=128)
    argument_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy_version: str = Field(min_length=1, max_length=100)
    expires_at: AwareDatetime
    decision: Literal["approve", "reject"]


class ApprovalSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID
    run_id: UUID
    tenant: str
    call: ToolCall
    argument_digest: str
    policy_version: str
    expires_at: AwareDatetime
    status: Literal["pending", "approved", "rejected", "expired", "cancelled", "executed"]
    decided_by: str | None = None
    ticket_id: str | None = None


class TicketGate(Protocol):
    async def run(self, incident: IncidentRequest, result: IncidentResult) -> None: ...


_gate: ContextVar[TicketGate | None] = ContextVar("ticket_gate", default=None)


def current_ticket_gate() -> TicketGate:
    gate = _gate.get()
    if gate is None:
        raise ApprovalError("DURABLE_APPROVAL_REQUIRED")
    return gate


@contextmanager
def ticket_scope(gate: TicketGate) -> Iterator[None]:
    token = _gate.set(gate)
    try:
        yield
    finally:
        _gate.reset(token)
