import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from runweave.app import create_app
from runweave.approvals import ApprovalError, TicketArguments
from runweave.core.settings import Settings
from runweave.identity import IdentityProvider
from runweave.security import ALL_SCOPES, PolicyDenied, Principal

ARGS = TicketArguments(service="payments", title="Synthetic incident", severity="low")


async def test_pydantic_ticket_defers_then_resumes_exact_arguments() -> None:
    pytest.importorskip("pydantic_ai")
    from runweave.agents.ticket import ticket_agent

    calls: list[TicketArguments] = []

    async def perform(arguments: TicketArguments) -> str:
        calls.append(arguments)
        return "sandbox-ticket:1"

    arguments, messages = await ticket_agent(ARGS, "call-1", perform)
    assert calls == []
    assert json.loads(arguments) == {"ticket": ARGS.model_dump()}
    assert messages is not None
    _, remaining = await ticket_agent(ARGS, "call-1", perform, messages)
    assert remaining is None and calls == [ARGS]
    with pytest.raises(ApprovalError, match="cannot be safely resumed"):
        await ticket_agent(
            ARGS.model_copy(update={"title": "Changed"}), "call-1", perform, messages
        )
    with pytest.raises(ApprovalError):
        await ticket_agent(ARGS, "call-2", perform, messages)
    assert calls == [ARGS]


async def test_memory_rejects_ticket_before_admission() -> None:
    app = create_app(Settings(worker_count=1))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as c:
            response = await c.post(
                "/v1/runs",
                json={"service": "payments", "description": "Synthetic", "create_ticket": True},
            )
            assert response.status_code == 503
            assert response.json()["detail"]["code"] == "DURABLE_APPROVAL_REQUIRED"


def test_execution_identity_rechecks_revocation_and_never_grants_new_scope() -> None:
    owner = Principal(tenant="a", subject="alice", scopes=ALL_SCOPES)
    settings = Settings(auth_mode="test", auth_test_identities={"synthetic-token-123": owner})
    provider = IdentityProvider(settings)
    reduced = owner.model_copy(update={"scopes": owner.scopes - {"tools:create_ticket"}})
    settings.auth_test_identities["synthetic-token-123"] = reduced
    with pytest.raises(PolicyDenied):
        provider.execution_principal(owner).require("tools:create_ticket")
    settings.auth_test_identities["synthetic-token-123"] = owner
    assert "tools:create_ticket" not in provider.execution_principal(reduced).scopes
    settings.auth_revoked_subjects = frozenset({("a", "alice")})
    with pytest.raises(PolicyDenied):
        provider.execution_principal(owner)
    settings.auth_revoked_subjects = frozenset()
    expired = owner.model_copy(update={"expires_at": datetime.now(UTC) - timedelta(seconds=1)})
    with pytest.raises(PolicyDenied):
        provider.execution_principal(expired)
    settings.auth_test_identities["synthetic-token-123"] = expired
    with pytest.raises(PolicyDenied):
        provider.execution_principal(owner)
    settings.auth_test_identities["synthetic-token-123"] = owner.model_copy(
        update={"scopes": owner.scopes - {"runs:submit"}}
    )
    with pytest.raises(PolicyDenied):
        provider.execution_principal(owner)
