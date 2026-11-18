"""HTTP agents in Python and TypeScript reach the proxy over a real socket.

RunWeave sends each HTTP agent its run's proxy binding in the run request; the agents use
the official SDKs unmodified apart from base URL and key.
"""

import asyncio
import json
import os
import shutil
import socket
import subprocess
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import openai
import pytest
import uvicorn
from agent_server import agent_server
from pydantic import BaseModel

from runweave.app import create_app
from runweave.core.settings import Settings
from runweave.sdk import RunContext, agent

ROOT = Path(__file__).resolve().parents[1]
COMPAT = ROOT / "tests" / "proxy_compat"
REQUIRE_ALL = os.environ.get("RUNWEAVE_CONTRACT_REQUIRE_ALL") == "1"
FIXTURES = {"version": 1, "rules": [
    {"when": {"contains": "weather", "has_tools": True},
     "reply": {"tool_calls": [{"name": "lookup_weather", "arguments": {"city": "Paris"}}]}},
    {"when": {"contains": "hello"}, "reply": {"text": "Hello from the RunWeave mock."}},
]}


class Ask(BaseModel):
    scenario: str


class Answer(BaseModel):
    text: str
    proxied: bool


@agent(name="py-http-sdk", version="1", input=Ask, output=Answer)
async def py_http_sdk(input: Ask, ctx: RunContext) -> Answer:
    assert ctx.proxy is not None
    client = openai.AsyncOpenAI(
        api_key=ctx.proxy.token, base_url=ctx.proxy.openai_base_url, max_retries=0
    )
    stream = await client.chat.completions.create(
        model="m", messages=[{"role": "user", "content": "hello"}], stream=True
    )
    text = "".join([c.choices[0].delta.content or "" async for c in stream if c.choices])
    return Answer(text=text, proxied=ctx.proxy.openai_base_url.endswith("/llm/v1"))


@asynccontextmanager
async def runweave_server(tmp_path: Path) -> AsyncIterator[tuple[str, httpx.AsyncClient]]:
    fixtures = tmp_path / "fixtures.json"
    fixtures.write_text(json.dumps(FIXTURES), encoding="utf-8")
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    url = f"http://127.0.0.1:{sock.getsockname()[1]}"
    app = create_app(Settings(proxy_mock_fixtures=fixtures, proxy_public_url=url), agents=[])
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        async with asyncio.timeout(15):
            while True:
                if server.started:
                    break
                await asyncio.sleep(0.02)
        async with httpx.AsyncClient(base_url=url, timeout=30) as client:
            yield url, client
    finally:
        server.should_exit = True
        await task
        sock.close()


async def run(client: httpx.AsyncClient, name: str) -> dict[str, Any]:
    response = await client.post("/v1/runs", json={"agent": name, "input": {"scenario": "all"}})
    assert response.status_code == 202, response.text
    status_url = response.json()["status_url"]
    async with asyncio.timeout(60):
        while True:
            body: dict[str, Any] = (await client.get(status_url)).json()
            if body["status"] in {"SUCCEEDED", "FAILED", "TIMED_OUT", "CANCELLED"}:
                return body
            await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_python_http_agent_receives_its_proxy_binding(tmp_path: Path) -> None:
    async with runweave_server(tmp_path) as (url, client), agent_server(py_http_sdk) as agent_url:
        assert (await client.post("/v1/agents/http", json={"url": agent_url})).status_code == 201
        result = await run(client, "py-http-sdk")
    assert result["status"] == "SUCCEEDED", result
    assert result["result"] == {"text": "Hello from the RunWeave mock.", "proxied": True}
    assert result["usage"]["requests"] == 1
    assert [s["title"] for s in result["steps"]] == ["Model call"]


@pytest.mark.asyncio
async def test_official_node_sdks_run_unmodified_through_the_proxy(tmp_path: Path) -> None:
    node = shutil.which("node")
    if not (node and (COMPAT / "node_modules" / "openai").is_dir()):
        reason = "Install the Node SDKs: npm --prefix tests/proxy_compat ci (after building sdk/typescript)"
        if REQUIRE_ALL:
            pytest.fail(reason)
        pytest.skip(reason)
    process = subprocess.Popen(
        [node, str(COMPAT / "agent.mjs")], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, cwd=COMPAT,
    )
    try:
        assert process.stdout is not None
        line = await asyncio.wait_for(asyncio.to_thread(process.stdout.readline), 30)
        assert line.startswith("RUNWEAVE_AGENT_READY "), line
        agent_url = line.split()[1]
        async with runweave_server(tmp_path) as (url, client):
            assert (await client.post("/v1/agents/http", json={"url": agent_url})).status_code == 201
            result = await run(client, "node-sdks")
    finally:
        process.kill()
        process.wait(timeout=10)
    assert result["status"] == "SUCCEEDED", result
    hello = "Hello from the RunWeave mock."
    data = result["result"]
    assert (data["chat"], data["chatStream"], data["response"], data["responseStream"]) == (hello,) * 4
    assert (data["claude"], data["claudeStream"]) == (hello, hello)
    assert data["claudeStreamUsage"] > 0
    assert data["tool"] == {"name": "lookup_weather", "arguments": {"city": "Paris"}}
    assert data["claudeTool"] == {"name": "lookup_weather", "input": {"city": "Paris"}}
    assert result["usage"]["requests"] == 8
    assert sum(s["title"] == "Model call" for s in result["steps"]) == 8
