"""Agent-declared approvals (`ctx.request_approval`) on disposable PostgreSQL.

A waiting agent keeps its worker slot and lease; its call awaits the decision in place.
Requests are bound to tenant, run, action key, argument digest, policy version and expiry.
Decisions are idempotent; opposing decisions conflict. A re-run that repeats the same
request gets the existing request or decision instead of a duplicate.
"""

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest
from pydantic import BaseModel, SecretStr
from runweave.storage.action_approvals import PostgresActionApprovals
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from runweave.agents.catalog import PythonAgent
from runweave.app import create_app
from runweave.core.settings import Settings
from runweave.sdk import RunContext, agent
from runweave.security import ALL_SCOPES, Principal

_DSN = "TEST_RUNWEAVE_POSTGRES_URL"
_MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"
REQUESTS: list[int] = []  # attempts that reached request_approval


class Refund(BaseModel):
    order: str
    amount: int
    expires_in: float = 30


class Outcome(BaseModel):
    approved: bool
    status: str
    decided_by: str | None
    attempt: int
    remaining_seconds: float


@agent(name="refunder", version="1.0.0", input=Refund, output=Outcome)
async def refunder(input: Refund, ctx: RunContext) -> Outcome:
    REQUESTS.append(ctx.attempt)
    decision = await ctx.request_approval(
        "issue_refund", {"order": input.order, "amount": input.amount}, expires_in=input.expires_in
    )
    return Outcome(
        approved=decision.approved,
        status=decision.status,
        decided_by=decision.decided_by,
        attempt=ctx.attempt,
        remaining_seconds=ctx.remaining_seconds(),
    )


AGENTS = [
    PythonAgent.from_function(refunder, target="tests.test_postgres_action_approvals:refunder")
]


def engine_for(dsn: str, schema: str) -> AsyncEngine:
    return create_async_engine(
        dsn, hide_parameters=True, connect_args={"server_settings": {"search_path": schema}}
    )


@pytest.fixture
async def database(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[tuple[str, AsyncEngine]]:
    dsn = os.environ.get(_DSN)
    if not dsn:
        pytest.skip(f"Set {_DSN} to run PostgreSQL approval tests")
    name = f"action_approvals_{uuid4().hex}"
    admin = create_async_engine(dsn, hide_parameters=True)
    engine = engine_for(dsn, name)
    async with admin.begin() as conn:
        await conn.execute(text(f"CREATE SCHEMA {name}"))
    try:
        paths = await asyncio.to_thread(lambda: sorted(_MIGRATIONS.glob("*.sql")))
        async with engine.begin() as conn:
            for path in paths:
                sql = await asyncio.to_thread(path.read_text, "utf-8")
                for statement in sql.split(";"):
                    if statement.strip():
                        await conn.execute(text(statement))
        monkeypatch.setattr(
            "runweave.core.runtime.postgres_engine", lambda settings: engine_for(dsn, name)
        )
        REQUESTS.clear()
        yield dsn, engine
    finally:
        await engine.dispose()
        async with admin.begin() as conn:
            await conn.execute(text(f"DROP SCHEMA {name} CASCADE"))
        await admin.dispose()


@asynccontextmanager
async def workspace(dsn: str, **overrides: Any) -> AsyncIterator[httpx.AsyncClient]:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        database_url=SecretStr(dsn),
        model_provider="fake",
        key_store="none",
        worker_count=overrides.pop("worker_count", 2),
        db_poll_seconds=0.05,
        lease_seconds=1,
        **overrides,
    )
    app = create_app(settings, agents=AGENTS)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://rw") as client,
    ):
        yield client


async def submit(client: httpx.AsyncClient, **body: Any) -> str:
    response = await client.post(
        "/v1/runs", json={"agent": "refunder", "input": {"order": "A-17", "amount": 40, **body}}
    )
    assert response.status_code == 202, response.text
    return str(response.json()["status_url"])


async def pending(client: httpx.AsyncClient, count: int = 1) -> list[dict[str, Any]]:
    async with asyncio.timeout(20):
        while True:
            body = (await client.get("/v1/approvals")).json()
            if len(body["approvals"]) >= count:
                return list(body["approvals"])
            await asyncio.sleep(0.05)


async def finished(client: httpx.AsyncClient, url: str) -> dict[str, Any]:
    async with asyncio.timeout(30):
        while True:
            run: dict[str, Any] = (await client.get(url)).json()
            if run["status"] in {"SUCCEEDED", "FAILED", "TIMED_OUT", "CANCELLED"}:
                return run
            await asyncio.sleep(0.05)


def decision(approval: dict[str, Any], choice: str, **changes: Any) -> dict[str, Any]:
    return {
        "decision": choice,
        "argument_digest": approval["argument_digest"],
        "policy_version": approval["policy_version"],
        "expires_at": approval["expires_at"],
        **changes,
    }


async def decide(
    client: httpx.AsyncClient, approval: dict[str, Any], choice: str
) -> httpx.Response:
    return await client.post(f"/v1/approvals/{approval['id']}", json=decision(approval, choice))


@pytest.mark.asyncio
async def test_approve_lets_the_waiting_agent_continue(database: tuple[str, AsyncEngine]) -> None:
    dsn, _ = database
    async with workspace(dsn) as client:
        url = await submit(client)
        (approval,) = await pending(client)
        assert approval["agent"] == "refunder" and approval["action"] == "issue_refund"
        assert approval["arguments"] == {"order": "A-17", "amount": 40}
        assert len(approval["argument_digest"]) == 64
        assert approval["policy_version"] == "agent:refunder@1.0.0"
        assert approval["status"] == "pending" and approval["run_id"] in url
        assert (await client.get(url)).json()["status"] == "WAITING_APPROVAL"
        assert (await client.get(f"/v1/approvals/{approval['id']}")).json()["id"] == approval["id"]

        first = await decide(client, approval, "approve")
        assert first.status_code == 200 and first.json()["status"] == "approved"
        again = await decide(client, approval, "approve")  # idempotent
        assert again.status_code == 200 and again.json()["status"] == "approved"
        opposing = await decide(client, approval, "reject")
        assert opposing.status_code == 409
        assert opposing.json()["detail"]["code"] == "APPROVAL_DECISION_CONFLICT"

        run = await finished(client, url)
    assert run["status"] == "SUCCEEDED", run
    assert run["result"]["approved"] is True and run["result"]["decided_by"] == "developer"
    assert REQUESTS == [1]


@pytest.mark.asyncio
async def test_reject_returns_to_the_agent(database: tuple[str, AsyncEngine]) -> None:
    dsn, _ = database
    async with workspace(dsn) as client:
        url = await submit(client)
        (approval,) = await pending(client)
        assert (await decide(client, approval, "reject")).status_code == 200
        run = await finished(client, url)
        assert (await client.get("/v1/approvals")).json()["approvals"] == []
    assert run["status"] == "SUCCEEDED"
    assert (run["result"]["approved"], run["result"]["status"]) == (False, "rejected")


@pytest.mark.asyncio
async def test_waiting_is_excluded_from_the_run_deadline(database: tuple[str, AsyncEngine]) -> None:
    dsn, engine = database
    async with workspace(dsn, run_timeout_seconds=1.5) as client:
        url = await submit(client)
        (approval,) = await pending(client)
        await asyncio.sleep(2.5)  # longer than the whole run deadline
        assert (await client.get(url)).json()["status"] == "WAITING_APPROVAL"
        assert (await decide(client, approval, "approve")).status_code == 200
        run = await finished(client, url)
    assert run["status"] == "SUCCEEDED", run
    assert 0 < run["result"]["remaining_seconds"] <= 1.5
    async with engine.connect() as conn:
        waited = await conn.scalar(text("SELECT approval_wait_seconds FROM runweave_runs"))
    assert waited is not None and waited >= 2


@pytest.mark.asyncio
async def test_expiry_times_the_run_out(database: tuple[str, AsyncEngine]) -> None:
    dsn, _ = database
    async with workspace(dsn) as client:
        url = await submit(client, expires_in=0.6)
        (approval,) = await pending(client)
        run = await finished(client, url)
        late = await decide(client, approval, "approve")
        stored = (await client.get(f"/v1/approvals/{approval['id']}")).json()
    assert run["status"] == "TIMED_OUT" and run["error"]["code"] == "APPROVAL_EXPIRED"
    assert late.status_code == 409 and late.json()["detail"]["code"] == "APPROVAL_NOT_PENDING"
    assert stored["status"] == "expired"


@pytest.mark.asyncio
async def test_expiry_is_capped_by_the_approval_ttl(database: tuple[str, AsyncEngine]) -> None:
    dsn, _ = database
    async with workspace(dsn, approval_ttl_seconds=1) as client:
        url = await submit(client, expires_in=3600)
        run = await finished(client, url)
    assert run["status"] == "TIMED_OUT" and run["error"]["code"] == "APPROVAL_EXPIRED"


@pytest.mark.asyncio
async def test_cancelling_a_waiting_run_cancels_its_request(
    database: tuple[str, AsyncEngine],
) -> None:
    dsn, _ = database
    async with workspace(dsn) as client:
        url = await submit(client)
        (approval,) = await pending(client)
        cancelled = await client.post(f"{url}/cancel")
        assert cancelled.json()["status"] == "CANCELLED"
        run = await finished(client, url)
        late = await decide(client, approval, "approve")
        stored = (await client.get(f"/v1/approvals/{approval['id']}")).json()
    assert run["status"] == "CANCELLED"
    assert late.status_code == 409 and stored["status"] == "cancelled"


@pytest.mark.asyncio
async def test_concurrent_decisions_resolve_to_one(database: tuple[str, AsyncEngine]) -> None:
    dsn, _ = database
    async with workspace(dsn) as client:
        url = await submit(client)
        (approval,) = await pending(client)
        responses = await asyncio.gather(
            decide(client, approval, "approve"), decide(client, approval, "reject")
        )
        codes = sorted(r.status_code for r in responses)
        assert codes == [200, 409]
        winner = next(r for r in responses if r.status_code == 200).json()["status"]
        run = await finished(client, url)
        repeat = "approve" if winner == "approved" else "reject"
        same = await asyncio.gather(*(decide(client, approval, repeat) for _ in range(3)))
    assert run["result"]["status"] == winner
    assert all(r.status_code == 200 for r in same)


@pytest.mark.asyncio
async def test_a_decision_is_bound_to_the_exact_request(database: tuple[str, AsyncEngine]) -> None:
    dsn, _ = database
    async with workspace(dsn) as client:
        await submit(client)
        (approval,) = await pending(client)
        for change in (
            {"argument_digest": "0" * 64},
            {"policy_version": "agent:refunder@9.9.9"},
            {"expires_at": "2099-01-01T00:00:00+00:00"},
        ):
            response = await client.post(
                f"/v1/approvals/{approval['id']}", json=decision(approval, "approve", **change)
            )
            assert response.status_code == 409, change
            assert response.json()["detail"]["code"] == "APPROVAL_BINDING_MISMATCH"
        unknown = await client.post(f"/v1/approvals/{uuid4()}", json=decision(approval, "approve"))
        assert unknown.status_code == 404
        await decide(client, approval, "reject")


@pytest.mark.asyncio
async def test_a_restarted_run_gets_the_existing_request_not_a_duplicate(
    database: tuple[str, AsyncEngine],
) -> None:
    dsn, engine = database
    async with workspace(dsn, worker_count=1) as client:
        url = await submit(client)
        (approval,) = await pending(client)
    # The workspace stopped while the agent waited, like a crash: the lease expires, and
    # the decision arrives while no worker is running.
    store = PostgresActionApprovals(engine)
    local = Principal(tenant="local", subject="developer", scopes=ALL_SCOPES)
    async with workspace(dsn, worker_count=1) as client:
        assert (await decide(client, approval, "approve")).status_code == 200
        run = await finished(client, url)
        listed = await store.list(local, status="all")
    assert run["status"] == "SUCCEEDED", run
    assert run["result"]["approved"] is True and run["result"]["attempt"] >= 2
    assert [a.id for a in listed] == [approval_id(approval)]
    assert REQUESTS[0] == 1 and REQUESTS[-1] == run["result"]["attempt"]


def approval_id(approval: dict[str, Any]) -> Any:
    from uuid import UUID

    return UUID(approval["id"])


@pytest.mark.asyncio
async def test_a_re_run_while_still_pending_waits_on_the_same_request(
    database: tuple[str, AsyncEngine],
) -> None:
    dsn, _ = database
    async with workspace(dsn, worker_count=1) as client:
        url = await submit(client)
        (first,) = await pending(client)
    async with workspace(dsn, worker_count=1) as client:
        async with asyncio.timeout(20):
            while True:  # until the re-run reaches request_approval again
                if len(REQUESTS) >= 2:
                    break
                await asyncio.sleep(0.05)
        (again,) = await pending(client)
        assert again["id"] == first["id"]
        assert (await decide(client, again, "approve")).status_code == 200
        run = await finished(client, url)
    assert run["status"] == "SUCCEEDED" and run["result"]["attempt"] >= 2


@pytest.mark.asyncio
async def test_pending_requests_are_bounded(database: tuple[str, AsyncEngine]) -> None:
    dsn, _ = database
    async with workspace(dsn, worker_count=4, approval_max_pending=1) as client:
        first = await submit(client, order="A-1")
        await pending(client)
        second = await submit(client, order="A-2")
        refused = await finished(client, second)
        (approval,) = await pending(client)
        await decide(client, approval, "approve")
        await finished(client, first)
    assert refused["status"] == "FAILED"
    assert refused["error"]["code"] == "APPROVAL_CAPACITY_EXCEEDED"


@pytest.mark.asyncio
async def test_other_tenants_cannot_see_or_decide(database: tuple[str, AsyncEngine]) -> None:
    dsn, engine = database
    store = PostgresActionApprovals(engine)
    stranger = Principal(tenant="other", subject="someone", scopes=ALL_SCOPES)
    async with workspace(dsn) as client:
        await submit(client)
        (approval,) = await pending(client)
        assert await store.list(stranger) == []
        assert await store.get(approval_id(approval), stranger) is None
        from runweave.action_approvals import ApprovalChoice

        choice = ApprovalChoice.model_validate(decision(approval, "approve"))
        assert await store.decide(approval_id(approval), stranger, choice) is None
        await decide(client, approval, "reject")
