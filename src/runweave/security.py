import json
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Annotated

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints

IdentityName = Annotated[str, StringConstraints(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:@-]{0,127}$")]
Scope = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_:.-]{0,63}$")]
ALL_SCOPES = frozenset(
    {
        "runs:submit",
        "runs:read",
        "runs:cancel",
        "tools:check_health",
        "tools:lookup_runbook",
        "tools:create_ticket",
        "approvals:decide",
        "memory:read",
        "memory:write",
        "memory:delete",
        "agents:delegate",
        "agents:manage",
        "tools:python_sandbox",
        "tools:browser_read",
        "tools:browser_private",
    }
)


class PolicyDenied(ValueError):
    def __init__(self) -> None:
        super().__init__("Execution policy denied the operation")


class TenantQuotaExceeded(Exception):
    pass


class Principal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant: IdentityName
    subject: IdentityName
    scopes: frozenset[Scope] = Field(max_length=32)
    expires_at: AwareDatetime | None = None

    def require(self, scope: str) -> None:
        if scope not in self.scopes or (
            self.expires_at is not None and datetime.now(UTC) >= self.expires_at
        ):
            raise PolicyDenied()

    def owns(self, owner: "Principal") -> bool:
        return (self.tenant, self.subject) == (owner.tenant, owner.subject)


LOCAL_PRINCIPAL = Principal(tenant="local", subject="developer", scopes=ALL_SCOPES)
_principal = ContextVar("runweave_principal", default=LOCAL_PRINCIPAL)


def current_principal() -> Principal:
    return _principal.get()


@contextmanager
def principal_scope(principal: Principal) -> Iterator[None]:
    token = _principal.set(principal)
    try:
        yield
    finally:
        _principal.reset(token)


class ContentPolicy:
    """Reject configured credentials at data boundaries without echoing the value."""

    def __init__(self, secrets: tuple[str, ...] = ()) -> None:
        self._secrets = tuple(
            encoded
            for value in secrets
            if value
            for encoded in (
                value,
                json.dumps(value, ensure_ascii=False)[1:-1],
                json.dumps(value, ensure_ascii=True)[1:-1],
            )
        )

    def check(self, text: str) -> None:
        if any(secret in text for secret in self._secrets):
            raise PolicyDenied()

    def redact(self, text: str) -> str:
        # Longest first so an escaped encoding never leaves a partial secret behind.
        for secret in sorted(self._secrets, key=len, reverse=True):
            text = text.replace(secret, "[REDACTED]")
        return text
