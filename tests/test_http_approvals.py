"""`request_approval` from HTTP agents (Python and TypeScript SDKs) on disposable PostgreSQL.

RunWeave runs over real TCP so the fixture agents can call back to it: each run request
carries an `approvals_url`, and the agent authenticates with its run attempt's token.
The same scenarios as for in-process Python agents: approve, reject, expiry, cancellation,
duplicate and concurrent decisions, waiting longer than the deadline, and a restarted
RunWeave whose re-run gets the existing request instead of a duplicate.
"""

import asyncio
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
import uvicorn
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine
from test_http_contract_suite import agent_url as agent_url
from test_postgres_action_approvals import database as database

from runweave.app import create_app
from runweave.core.settings import Settings

TERMINAL = {"SUCCEEDED", "FAILED", "TIMED_OUT", "CANCELLED"}


@asynccontextmanager
async def runweave(dsn: str, agent: str, **overrides: Any) -> AsyncIterator[httpx.AsyncClient]:
    """A durable RunWeave on a free loopback port, so HTTP agents can reach it."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    url = f"http://127.0.0.1:{sock.getsockname()[1]}"
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        database_url=SecretStr(dsn),
        model_provider="fake",
        key_store="none",
        http_agents=(agent,),
        proxy_public_url=url,
        worker_count=overrides.pop("worker_count", 2),
        db_poll_seconds=0.05,
        lease_seconds=1,
        http_agent_retry_base_delay_seconds=0.01,
        http_agent_retry_max_delay_seconds=0.05,
        **overrides,
    )
    app = create_app(settings, agents=[])
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
    serving = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        async with asyncio.timeout(20):
            while not server.started:
                if serving.done():
                    serving.result()
                await asyncio.sleep(0.02)
        async with httpx.AsyncClient(base_url=url, timeout=30, trust_env=False) as client:
            yield client
    finally:
        server.should_exit = True
        await asyncio.gather(serving, return_exceptions=True)
        sock.close()


async def submit(client: httpx.AsyncClient, **body: Any) -> str:
    response = await client.post(
        "/v1/runs", json={"agent": "contract-approver", "input": {"order": "A-17", **body}}
    )
    assert response.status_code == 202, response.text
    return str(response.json()["status_url"])


async def pending(client: httpx.AsyncClient, status: str = "pending") -> list[dict[str, Any]]:
    async with asyncio.timeout(20):
        while True:
            body = (await client.get("/v1/approvals", params={"status": status})).json()
            if body["approvals"]:
                return list(body["approvals"])
            await asyncio.sleep(0.05)


async def finished(client: httpx.AsyncClient, url: str) -> dict[str, Any]:
    async with asyncio.timeout(30):
        while True:
            run: dict[str, Any] = (await client.get(url)).json()
            if run["status"] in TERMINAL:
                return run
            await asyncio.sleep(0.05)


async def decide(
    client: httpx.AsyncClient, approval: dict[str, Any], choice: str
) -> httpx.Response:
    return await client.post(
        f"/v1/approvals/{approval['id']}",
        json={
            "decision": choice,
            "argument_digest": approval["argument_digest"],
            "policy_version": approval["policy_version"],
            "expires_at": approval["expires_at"],
        },
    )


@pytest.mark.asyncio
async def test_approve_and_reject_reach_the_http_agent(
    database: tuple[str, AsyncEngine], agent_url: str
) -> None:
    dsn, _ = database
    async with runweave(dsn, agent_url) as client:
        url = await submit(client)
        (approval,) = await pending(client)
        assert approval["agent"] == "contract-approver" and approval["action"] == "issue_refund"
        assert approval["arguments"] == {"order": "A-17"}
        assert approval["policy_version"] == "agent:contract-approver@1.0.0"
        assert (await client.get(url)).json()["status"] == "WAITING_APPROVAL"
        assert (await decide(client, approval, "approve")).status_code == 200
        assert (await decide(client, approval, "approve")).status_code == 200
        assert (await decide(client, approval, "reject")).status_code == 409
        approved = await finished(client, url)

        url = await submit(client, order="B-2")
        (second,) = await pending(client)
        assert (await decide(client, second, "reject")).status_code == 200
        rejected = await finished(client, url)
    assert approved["status"] == "SUCCEEDED", approved
    assert approved["result"]["approved"] is True and approved["result"]["decided_by"]
    assert rejected["status"] == "SUCCEEDED"
    assert (rejected["result"]["approved"], rejected["result"]["status"]) == (False, "rejected")


@pytest.mark.asyncio
async def test_waiting_pauses_the_deadline_on_both_sides(
    database: tuple[str, AsyncEngine], agent_url: str
) -> None:
    dsn, _ = database
    async with runweave(dsn, agent_url, run_timeout_seconds=2) as client:
        url = await submit(client)
        (approval,) = await pending(client)
        await asyncio.sleep(3)  # longer than the run deadline RunWeave sent the agent
        assert (await client.get(url)).json()["status"] == "WAITING_APPROVAL"
        assert (await decide(client, approval, "approve")).status_code == 200
        run = await finished(client, url)
    assert run["status"] == "SUCCEEDED", run
    assert 0 < run["result"]["remaining_ms"] <= 2000


@pytest.mark.asyncio
async def test_expiry_times_the_run_out(database: tuple[str, AsyncEngine], agent_url: str) -> None:
    dsn, _ = database
    async with runweave(dsn, agent_url) as client:
        url = await submit(client, expires_in=0.6)
        run = await finished(client, url)
        (approval,) = await pending(client, status="all")
    assert run["status"] == "TIMED_OUT" and run["error"]["code"] == "APPROVAL_EXPIRED", run
    assert approval["status"] == "expired"


@pytest.mark.asyncio
async def test_cancelling_stops_the_waiting_agent(
    database: tuple[str, AsyncEngine], agent_url: str
) -> None:
    dsn, _ = database
    async with runweave(dsn, agent_url) as client:
        url = await submit(client)
        (approval,) = await pending(client)
        assert (await client.post(f"{url}/cancel")).json()["status"] == "CANCELLED"
        run = await finished(client, url)
        late = await decide(client, approval, "approve")
        (stored,) = await pending(client, status="all")
    assert run["status"] == "CANCELLED"
    assert late.status_code == 409 and stored["status"] == "cancelled"


@pytest.mark.asyncio
async def test_concurrent_decisions_resolve_to_one(
    database: tuple[str, AsyncEngine], agent_url: str
) -> None:
    dsn, _ = database
    async with runweave(dsn, agent_url) as client:
        url = await submit(client)
        (approval,) = await pending(client)
        responses = await asyncio.gather(
            decide(client, approval, "approve"), decide(client, approval, "reject")
        )
        assert sorted(r.status_code for r in responses) == [200, 409]
        winner = next(r for r in responses if r.status_code == 200).json()["status"]
        run = await finished(client, url)
    assert run["status"] == "SUCCEEDED" and run["result"]["status"] == winner


@pytest.mark.asyncio
async def test_a_restarted_runweave_reuses_the_request(
    database: tuple[str, AsyncEngine], agent_url: str
) -> None:
    dsn, _ = database
    async with runweave(dsn, agent_url, worker_count=1) as client:
        url = await submit(client, order="RESTART-1")
        (first,) = await pending(client)
    # RunWeave stopped while the agent waited; a new instance re-runs the run after the
    # lease expires, and the agent repeats its request.
    async with runweave(dsn, agent_url, worker_count=1) as client:
        async with asyncio.timeout(20), httpx.AsyncClient(trust_env=False) as fixture:
            while True:
                seen = (await fixture.get(f"{agent_url}/fixture/events")).json()["approvals"]
                if sum(item["order"] == "RESTART-1" for item in seen) >= 2:
                    break
                await asyncio.sleep(0.05)
        listed = (await client.get("/v1/approvals", params={"status": "all"})).json()
        assert [a["id"] for a in listed["approvals"]] == [first["id"]]
        assert (await decide(client, first, "approve")).status_code == 200
        run = await finished(client, url)
    assert run["status"] == "SUCCEEDED", run
    assert run["result"]["approved"] is True and run["result"]["attempt"] >= 2
