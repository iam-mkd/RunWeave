"""`runweave approvals`: list, show, approve and reject agent requests from a terminal."""

import asyncio
import json
import os
import socket
import subprocess
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from pydantic import BaseModel, SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine
from test_postgres_action_approvals import database as database

from runweave.agents.catalog import PythonAgent
from runweave.app import create_app
from runweave.core.settings import Settings
from runweave.sdk import RunContext, agent

ROOT = Path(__file__).resolve().parents[1]


class Order(BaseModel):
    order: str


class Done(BaseModel):
    approved: bool


@agent(name="cli-refund", version="1.0.0", input=Order, output=Done)
async def cli_refund(input: Order, ctx: RunContext) -> Done:
    decision = await ctx.request_approval("issue_refund", {"order": input.order, "amount": 40})
    return Done(approved=decision.approved)


@asynccontextmanager
async def server(dsn: str | None) -> AsyncIterator[str]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    url = f"http://127.0.0.1:{sock.getsockname()[1]}"
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        database_url=SecretStr(dsn) if dsn else None,
        model_provider="fake",
        key_store="none",
        proxy_public_url=url,
        worker_count=2,
        db_poll_seconds=0.05,
        lease_seconds=1,
    )
    target = "tests.test_approvals_cli:cli_refund"
    app = create_app(settings, agents=[PythonAgent.from_function(cli_refund, target=target)])
    instance = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
    serving = asyncio.create_task(instance.serve(sockets=[sock]))
    try:
        async with asyncio.timeout(20):
            while True:
                if instance.started:
                    break
                await asyncio.sleep(0.02)
        yield url
    finally:
        instance.should_exit = True
        await asyncio.gather(serving, return_exceptions=True)
        sock.close()


def cli(*args: str, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("RUNWEAVE_")}
    env["PYTHONPATH"] = str(ROOT / "src")
    return subprocess.run(
        [sys.executable, "-m", "runweave.cli", "approvals", *args],
        env=env,
        input=stdin,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


async def run_cli(*args: str, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    return await asyncio.to_thread(cli, *args, stdin=stdin)


async def submit(client: httpx.AsyncClient, order: str) -> str:
    response = await client.post(
        "/v1/runs", json={"agent": "cli-refund", "input": {"order": order}}
    )
    assert response.status_code == 202, response.text
    return str(response.json()["status_url"])


async def first_pending(client: httpx.AsyncClient) -> dict[str, Any]:
    async with asyncio.timeout(20):
        while True:
            approvals = (await client.get("/v1/approvals")).json()["approvals"]
            if approvals:
                return dict(approvals[0])
            await asyncio.sleep(0.05)


async def outcome(client: httpx.AsyncClient, url: str) -> dict[str, Any]:
    async with asyncio.timeout(20):
        while True:
            run: dict[str, Any] = (await client.get(url)).json()
            if run["status"] not in {"QUEUED", "RUNNING", "WAITING_APPROVAL"}:
                return run
            await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_list_show_approve_and_reject(database: tuple[str, AsyncEngine]) -> None:
    dsn, _ = database
    async with server(dsn) as url, httpx.AsyncClient(base_url=url, trust_env=False) as client:
        run_url = await submit(client, "A-17")
        approval = await first_pending(client)

        listed = await run_cli("list", "--server", url)
        assert listed.returncode == 0, listed.stderr
        assert approval["id"] in listed.stdout and "issue_refund" in listed.stdout
        assert "cli-refund" in listed.stdout

        shown = await run_cli("show", approval["id"], "--server", url)
        assert shown.returncode == 0, shown.stderr
        assert '"order": "A-17"' in shown.stdout and approval["argument_digest"] in shown.stdout
        as_json = await run_cli("show", approval["id"], "--server", url, "--json")
        assert json.loads(as_json.stdout)["arguments"] == {"order": "A-17", "amount": 40}

        unconfirmed = await run_cli("approve", approval["id"], "--server", url, stdin="")
        assert unconfirmed.returncode == 2 and "--yes" in unconfirmed.stderr
        declined = await run_cli("approve", approval["id"], "--server", url, stdin="n\n")
        assert declined.returncode == 2

        approved = await run_cli("approve", approval["id"], "--server", url, stdin="y\n")
        assert approved.returncode == 0, approved.stderr
        assert '"order": "A-17"' in approved.stdout  # the exact arguments are shown first
        assert "Approved" in approved.stdout
        assert (await outcome(client, run_url))["result"] == {"approved": True}
        again = await run_cli("approve", approval["id"], "--server", url, "--yes")
        assert again.returncode == 0
        opposite = await run_cli("reject", approval["id"], "--server", url, "--yes")
        assert opposite.returncode == 1 and "APPROVAL_DECISION_CONFLICT" in opposite.stderr

        second_url = await submit(client, "B-2")
        second = await first_pending(client)
        rejected = await run_cli("reject", second["id"], "--server", url, "--yes")
        assert rejected.returncode == 0, rejected.stderr
        assert (await outcome(client, second_url))["result"] == {"approved": False}
        everything = await run_cli("list", "--all", "--server", url)
        assert "approved" in everything.stdout and "rejected" in everything.stdout


@pytest.mark.asyncio
async def test_without_a_database_or_a_server() -> None:
    async with server(None) as url:
        listed = await run_cli("list", "--server", url)
        missing = await run_cli(
            "approve", "6f1d2c3b-4a59-4e8f-9d0a-1b2c3d4e5f60", "--server", url, "--yes"
        )
    assert listed.returncode == 0 and "RUNWEAVE_DATABASE_URL" in listed.stdout
    assert missing.returncode == 2 and "not found" in missing.stderr.lower()
    unreachable = await run_cli("list", "--server", "http://127.0.0.1:9")
    assert unreachable.returncode == 2 and "Traceback" not in unreachable.stderr
    invalid = await run_cli("show", "not-an-id", "--server", "http://127.0.0.1:9")
    assert invalid.returncode == 2
