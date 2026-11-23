"""One contract suite for every agent SDK, run end to end through RunWeave.

See tests/contract/README.md for the fixture behavior each SDK implements.
"""

import asyncio
import os
import queue
import shutil
import subprocess
import sys
import threading
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from runweave.app import create_app
from runweave.core.settings import Settings

ROOT = Path(__file__).resolve().parents[1]
TYPESCRIPT_SDK = ROOT / "sdk" / "typescript" / "dist" / "index.js"
FIXTURES = {
    "python": [sys.executable, str(ROOT / "tests" / "contract" / "python_agent.py")],
    "typescript": [
        shutil.which("node") or "node",
        str(ROOT / "tests" / "contract" / "typescript_agent.mjs"),
    ],
}
# CI sets this so a missing Node or unbuilt SDK fails instead of skipping.
REQUIRE_ALL = os.environ.get("RUNWEAVE_CONTRACT_REQUIRE_ALL") == "1"
TERMINAL = {"SUCCEEDED", "FAILED", "TIMED_OUT", "CANCELLED"}


def _start(command: list[str]) -> tuple[subprocess.Popen[str], str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("RUNWEAVE_")}
    process = subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, env=env, cwd=ROOT
    )
    lines: queue.Queue[str] = queue.Queue()
    assert process.stdout is not None
    stdout = process.stdout

    def pump() -> None:
        for line in stdout:
            lines.put(line)

    threading.Thread(target=pump, daemon=True).start()
    try:
        while True:
            line = lines.get(timeout=30)
            if line.startswith("RUNWEAVE_AGENT_READY "):
                return process, line.split()[1]
    except queue.Empty:
        process.kill()
        raise RuntimeError(f"Fixture agent did not start: {command}") from None


@pytest.fixture(scope="module", params=sorted(FIXTURES))
def agent_url(request: pytest.FixtureRequest) -> Iterator[str]:
    if request.param == "typescript" and not (shutil.which("node") and TYPESCRIPT_SDK.is_file()):
        reason = (
            "Build the TypeScript SDK: npm --prefix sdk/typescript ci "
            "&& npm --prefix sdk/typescript run build"
        )
        if REQUIRE_ALL:
            pytest.fail(reason)
        pytest.skip(reason)
    process, url = _start(FIXTURES[request.param])
    try:
        yield url
    finally:
        process.kill()
        process.wait(timeout=10)


@asynccontextmanager
async def runweave(url: str, **settings: Any) -> AsyncIterator[httpx.AsyncClient]:
    config = Settings(
        http_agents=(url,),
        run_timeout_seconds=settings.pop("run_timeout_seconds", 3),
        http_agent_response_max_bytes=262144,
        http_agent_retry_base_delay_seconds=0.01,
        http_agent_retry_max_delay_seconds=0.05,
        **settings,
    )
    app: FastAPI = create_app(config, agents=[])
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        yield client


async def start(client: httpx.AsyncClient, agent: str, **payload: Any) -> str:
    response = await client.post(
        "/v1/runs", json={"agent": agent, "input": {"text": "contract", **payload}}
    )
    assert response.status_code == 202, response.text
    return str(response.json()["status_url"])


async def wait(client: httpx.AsyncClient, url: str, states: set[str] = TERMINAL) -> dict[str, Any]:
    async with asyncio.timeout(15):
        while True:
            body: dict[str, Any] = (await client.get(url)).json()
            if body["status"] in states:
                return body
            await asyncio.sleep(0.02)


async def events(url: str) -> dict[str, Any]:
    async with httpx.AsyncClient() as http:
        body: dict[str, Any] = (await http.get(f"{url}/fixture/events")).json()
        return body


async def eventually_cancelled(url: str, text: str) -> None:
    async with asyncio.timeout(5):
        while True:
            if text in (await events(url))["cancelled"]:
                return
            await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_agents_are_discovered_with_their_schemas(agent_url: str) -> None:
    async with runweave(agent_url) as client:
        agents = {a["name"]: a for a in (await client.get("/v1/agents")).json()["agents"]}
    echo = agents["contract-echo"]
    assert (echo["kind"], echo["target"], echo["version"]) == ("http", agent_url, "1.0.0")
    assert echo["input_schema"]["properties"]["text"]["maxLength"] == 100
    assert echo["removable"] is False


@pytest.mark.asyncio
async def test_success_returns_output_and_steps(agent_url: str) -> None:
    async with runweave(agent_url) as client:
        run = await wait(client, await start(client, "contract-echo"))
    assert run["status"] == "SUCCEEDED", run
    assert run["result"] == {"reply": "contract", "attempt": 1}
    assert [(s["title"], s["status"]) for s in run["steps"]] == [("Echo", "succeeded")]


@pytest.mark.asyncio
async def test_input_is_validated_before_admission(agent_url: str) -> None:
    async with runweave(agent_url) as client:
        response = await client.post(
            "/v1/runs", json={"agent": "contract-echo", "input": {"text": "", "delay_ms": -1}}
        )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "INVALID_INPUT"
    assert {tuple(e["loc"]) for e in response.json()["detail"]["errors"]} == {
        ("text",),
        ("delay_ms",),
    }


@pytest.mark.asyncio
async def test_timeout_fails_the_run_and_cancels_the_agent(agent_url: str) -> None:
    async with runweave(agent_url, run_timeout_seconds=1) as client:
        run = await wait(
            client, await start(client, "contract-echo", text="timeout", delay_ms=30000)
        )
    assert run["status"] == "TIMED_OUT"
    await eventually_cancelled(agent_url, "timeout")


@pytest.mark.asyncio
async def test_cancellation_reaches_the_agent(agent_url: str) -> None:
    async with runweave(agent_url, run_timeout_seconds=30) as client:
        url = await start(client, "contract-echo", text="cancel", delay_ms=30000)
        await wait(client, url, {"RUNNING"})
        await asyncio.sleep(0.2)
        response = await client.post(f"{url}/cancel")
        assert response.status_code in {200, 202}, response.text
        run = await wait(client, url)
    assert run["status"] == "CANCELLED"
    await eventually_cancelled(agent_url, "cancel")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("agent", "code"),
    [
        ("contract-crash", "RUN_FAILED"),
        ("contract-malformed", "AGENT_RESPONSE_INVALID"),
        ("contract-oversized", "AGENT_RESPONSE_TOO_LARGE"),
        ("contract-redirect", "AGENT_REDIRECT_DENIED"),
        ("contract-server-error", "AGENT_SERVER_ERROR"),
        ("contract-unavailable", "AGENT_UNAVAILABLE"),
    ],
)
async def test_failures_are_classified(agent_url: str, agent: str, code: str) -> None:
    async with runweave(agent_url) as client:
        run = await wait(client, await start(client, agent))
    assert run["status"] == "FAILED"
    assert run["error"]["code"] == code
    assert "secret" not in str(run) and "internal" not in str(run)


@pytest.mark.asyncio
async def test_only_transient_failures_are_retried(agent_url: str) -> None:
    before = (await events(agent_url))["requests"]
    async with runweave(agent_url, http_agent_retry_attempts=3) as client:
        for agent in ("contract-unavailable", "contract-server-error", "contract-redirect"):
            await wait(client, await start(client, agent))
    after = (await events(agent_url))["requests"]

    def sent(name: str) -> int:
        return int(after.get(name, 0)) - int(before.get(name, 0))

    assert sent("contract-unavailable") == 3
    assert sent("contract-server-error") == 1
    assert sent("contract-redirect") == 1


@pytest.mark.asyncio
async def test_approvals_need_durable_mode(agent_url: str) -> None:
    async with runweave(agent_url) as client:
        response = await client.post(
            "/v1/runs", json={"agent": "contract-approver", "input": {"order": "memory"}}
        )
        assert response.status_code == 202, response.text
        run = await wait(client, response.json()["status_url"])
    assert run["status"] == "FAILED"
    assert run["error"]["code"] == "DURABLE_APPROVAL_REQUIRED"


def _seed_that_loses_the_first_write() -> int:
    from runweave.proxy.faults import BUILT_IN_PROFILES, FaultPlan

    for seed in range(100):
        faults = FaultPlan(BUILT_IN_PROFILES["lost-writes"], seed).effect_faults()
        if faults is not None and faults.lost(0, "create_ticket"):
            return seed
    raise AssertionError("no seed loses the first write")


async def _ticket(agent_url: str, order: str, *, use_key: bool) -> dict[str, Any]:
    seed = _seed_that_loses_the_first_write()
    async with runweave(agent_url, proxy_fault_profile="lost-writes", proxy_fault_seed=seed) as c:
        response = await c.post(
            "/v1/runs",
            json={"agent": "contract-ticketing", "input": {"order": order, "use_key": use_key}},
        )
        assert response.status_code == 202, response.text
        return await wait(c, response.json()["status_url"])


@pytest.mark.asyncio
async def test_effect_faults_reach_the_agent_and_its_effect_steps_come_back(
    agent_url: str,
) -> None:
    from runweave.agent_effects import duplicate_writes
    from runweave.proxy.faults import BUILT_IN_PROFILES, FaultPlan

    order = f"keyed-{agent_url.rsplit(':', 1)[1]}"
    run = await _ticket(agent_url, order, use_key=True)
    assert run["status"] == "SUCCEEDED", run
    effects = [step for step in run["steps"] if step["kind"] == "effect"]
    # Every SDK draws the same faults from the seed as RunWeave's own Python agents do.
    faults = FaultPlan(
        BUILT_IN_PROFILES["lost-writes"], _seed_that_loses_the_first_write()
    ).effect_faults()
    assert faults is not None
    lost = [faults.lost(n, "create_ticket") for n in range(len(effects))]
    assert [step["fault"] == "response_lost" for step in effects] == lost
    assert lost[0] and run["result"]["tries"] == len(effects)
    assert all(step["effect"] == "create_ticket" and step["resource"] for step in effects)
    assert len({step["operation"] for step in effects}) == 1
    assert duplicate_writes(run["steps"]) == []
    assert len((await events(agent_url))["tickets"][order]) == 1


@pytest.mark.asyncio
async def test_a_retry_without_a_key_writes_twice_and_runweave_sees_it(agent_url: str) -> None:
    from runweave.agent_effects import duplicate_writes

    order = f"unkeyed-{agent_url.rsplit(':', 1)[1]}"
    run = await _ticket(agent_url, order, use_key=False)
    assert run["status"] == "SUCCEEDED", run
    assert duplicate_writes(run["steps"]) == ["create_ticket"]
    assert len((await events(agent_url))["tickets"][order]) > 1
