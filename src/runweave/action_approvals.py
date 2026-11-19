"""Agent-declared approvals: `ctx.request_approval(action, arguments, expires_in)`.

A request is bound to tenant, run, a stable action key, the SHA-256 of the canonical JSON
arguments, a policy version (`agent:<name>@<version>`) and an expiry. The calling agent
waits in place and keeps its worker slot; its run deadline is paused while it waits. A
re-run that repeats the same request (same action and arguments) receives the existing
request or decision. RunWeave does not make the approved effect itself idempotent; that
stays the agent's responsibility.

Only durable mode (PostgreSQL) can bind a gate; without one the call fails with
`DURABLE_APPROVAL_REQUIRED`.
"""

import hashlib
import json
import re
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Protocol
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from runweave.sdk import AgentError

ACTION_PATTERN = re.compile(r"^[a-z][a-z0-9_.:-]{0,99}$")
MAX_ARGUMENT_BYTES = 65536
Status = Literal["pending", "approved", "rejected", "expired", "cancelled"]


class ApprovalUnavailable(AgentError):
    code = "DURABLE_APPROVAL_REQUIRED"

    def __init__(self) -> None:
        ValueError.__init__(
            self, "Approvals need durable mode: set RUNWEAVE_DATABASE_URL to a PostgreSQL database"
        )


class ApprovalExpired(AgentError):
    """No decision before the request's expiry; uncaught, the run ends TIMED_OUT."""

    code = "APPROVAL_EXPIRED"


class ApprovalRefused(AgentError):
    """The request could not be recorded (for example, too many are pending)."""

    def __init__(self, code: str) -> None:
        self.code = code
        ValueError.__init__(self, code)


@dataclass(frozen=True)
class Approval:
    """A decision returned to the agent. A rejection is returned, not raised."""

    id: UUID
    status: Literal["approved", "rejected"]
    decided_by: str | None
    decided_at: datetime | None

    @property
    def approved(self) -> bool:
        return self.status == "approved"


class ActionApproval(BaseModel):
    """One request as reviewers see it: the exact arguments and its binding."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    run_id: UUID
    tenant: str
    agent: str
    action: str
    arguments: Any
    argument_digest: str
    policy_version: str
    status: Status
    created_at: AwareDatetime
    expires_at: AwareDatetime
    decided_by: str | None = None
    decided_at: AwareDatetime | None = None


class ApprovalChoice(BaseModel):
    """A decision, valid only for the exact request it names."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    decision: Literal["approve", "reject"]
    argument_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy_version: str = Field(min_length=1, max_length=100)
    expires_at: AwareDatetime


def canonical(arguments: Any) -> str:
    try:
        text = json.dumps(arguments, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError):
        raise ValueError("Approval arguments must be JSON values") from None
    if len(text.encode()) > MAX_ARGUMENT_BYTES:
        raise ValueError(f"Approval arguments are limited to {MAX_ARGUMENT_BYTES} bytes")
    return text


def argument_digest(arguments: Any) -> str:
    return hashlib.sha256(canonical(arguments).encode()).hexdigest()


def check_action(action: str) -> str:
    if not isinstance(action, str) or not ACTION_PATTERN.match(action):
        raise ValueError("Action must match ^[a-z][a-z0-9_.:-]{0,99}$, for example issue_refund")
    return action


class ApprovalGate(Protocol):
    async def request(self, action: str, arguments: Any, expires_in: float | None) -> Approval: ...


_gate: ContextVar[ApprovalGate | None] = ContextVar("action_approval_gate", default=None)


def current_gate() -> ApprovalGate:
    gate = _gate.get()
    if gate is None:
        raise ApprovalUnavailable()
    return gate


@contextmanager
def approval_scope(gate: ApprovalGate) -> Iterator[None]:
    token = _gate.set(gate)
    try:
        yield
    finally:
        _gate.reset(token)
