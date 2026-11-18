"""Registering HTTP agents from the API at runtime, and at startup from settings."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
from agent_server import agent_server
from fastapi import FastAPI
from pydantic import BaseModel, Field

from runweave.app import create_app
from runweave.core.settings import Settings
from runweave.sdk import RunContext, agent


class Ask(BaseModel):
    text: str = Field(min_length=1, max_length=40)


class Reply(BaseModel):
    reply: str


@agent(name="remote-upper", version="1.0.0", input=Ask, output=Reply, description="Uppercase")
async def upper(input: Ask, ctx: RunContext) -> Reply:
    async with ctx.step("Uppercase"):
        return Reply(reply=input.text.upper())


@agent(name="remote-upper", version="2.0.0", input=Ask, output=Reply)
async def upper_v2(input: Ask, ctx: RunContext) -> Reply:
    return Reply(reply=input.text.upper() + "!")


@agent(name="echo", version="1", input=Ask, output=Reply)
async def clashing(input: Ask, ctx: RunContext) -> Reply:
    return Reply(reply=input.text)


@agent(name="echo", version="1", input=Ask, output=Reply)
async def local_echo(input: Ask, ctx: RunContext) -> Reply:
    return Reply(reply=input.text)


@asynccontextmanager
async def runweave(**settings: Any) -> AsyncIterator[httpx.AsyncClient]:
    from runweave.agents.catalog import PythonAgent

    app: FastAPI = create_app(
        Settings(**settings),
        agents=[
            PythonAgent.from_function(local_echo, target="tests.test_http_agents_api:local_echo")
        ],
    )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        yield client


async def finished(client: httpx.AsyncClient, url: str) -> dict[str, Any]:
    async with asyncio.timeout(10):
        while True:
            body: dict[str, Any] = (await client.get(url)).json()
            if body["status"] in {"SUCCEEDED", "FAILED", "TIMED_OUT", "CANCELLED"}:
                return body
            await asyncio.sleep(0.02)


@pytest.mark.asyncio
async def test_check_reports_the_agents_without_registering_them() -> None:
    async with agent_server(upper) as url, runweave() as client:
        checked = await client.post("/v1/agents/http/check", json={"url": url + "/"})
        listed = (await client.get("/v1/agents")).json()["agents"]
    assert checked.status_code == 200, checked.text
    body = checked.json()
    assert body["url"] == url
    assert body["agents"] == [
        {
            "name": "remote-upper",
            "version": "1.0.0",
            "description": "Uppercase",
            "max_steps": 50,
            "conflict": False,
        }
    ]
    assert "remote-upper" not in {a["name"] for a in listed}


@pytest.mark.asyncio
async def test_registered_http_agents_run_and_can_be_removed() -> None:
    async with agent_server(upper) as url, runweave() as client:
        registered = await client.post("/v1/agents/http", json={"url": url})
        assert registered.status_code == 201, registered.text
        assert [a["name"] for a in registered.json()["agents"]] == ["remote-upper"]
        listed = {a["name"]: a for a in (await client.get("/v1/agents")).json()["agents"]}
        submitted = await client.post(
            "/v1/runs", json={"agent": "remote-upper", "input": {"text": "hello"}}
        )
        run = await finished(client, submitted.json()["status_url"])
        removed = await client.delete("/v1/agents/remote-upper")
        after = {a["name"] for a in (await client.get("/v1/agents")).json()["agents"]}
        rejected = await client.post(
            "/v1/runs", json={"agent": "remote-upper", "input": {"text": "hello"}}
        )
    assert listed["remote-upper"]["kind"] == "http"
    assert listed["remote-upper"]["removable"] is True
    assert listed["remote-upper"]["target"] == url
    assert run["status"] == "SUCCEEDED" and run["result"] == {"reply": "HELLO"}
    assert [s["title"] for s in run["steps"]] == ["Uppercase"]
    assert removed.status_code == 204
    assert "remote-upper" not in after
    assert rejected.status_code == 422 and rejected.json()["detail"]["code"] == "UNKNOWN_AGENT"


@pytest.mark.asyncio
async def test_registering_the_same_url_again_refreshes_its_agents() -> None:
    async with runweave() as client:
        async with agent_server(upper) as url:
            first = await client.post("/v1/agents/http", json={"url": url})
        async with agent_server(upper_v2, port=int(url.rsplit(":", 1)[1])) as same:
            second = await client.post("/v1/agents/http", json={"url": same})
            listed = {a["name"]: a for a in (await client.get("/v1/agents")).json()["agents"]}
    assert (first.status_code, second.status_code) == (201, 201)
    assert listed["remote-upper"]["version"] == "2.0.0"
    assert listed["remote-upper"]["target"] == url


@pytest.mark.asyncio
async def test_names_already_registered_elsewhere_are_a_conflict() -> None:
    async with agent_server(clashing) as url, runweave() as client:
        checked = await client.post("/v1/agents/http/check", json={"url": url})
        response = await client.post("/v1/agents/http", json={"url": url})
    assert checked.json()["agents"][0]["conflict"] is True
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "AGENT_NAME_CONFLICT"


@pytest.mark.asyncio
async def test_registration_limit_is_enforced() -> None:
    async with agent_server(upper) as url, runweave(http_agent_max_registered=1) as client:
        assert (await client.post("/v1/agents/http", json={"url": url})).status_code == 201
        async with agent_server(_other_agent) as other:
            response = await client.post("/v1/agents/http", json={"url": other})
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "AGENT_LIMIT_REACHED"


@agent(name="remote-other", version="1", input=Ask, output=Reply)
async def _other_agent(input: Ask, ctx: RunContext) -> Reply:
    return Reply(reply="other")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("url", "status", "code"),
    [
        ("http://169.254.169.254/latest", 422, "AGENT_URL_NOT_ALLOWED"),
        ("http://example.com", 422, "AGENT_URL_NOT_ALLOWED"),
        ("file:///etc/passwd", 422, "AGENT_URL_NOT_ALLOWED"),
        ("http://127.0.0.1:1", 502, "AGENT_UNREACHABLE"),
    ],
)
async def test_unsafe_or_unreachable_urls_are_rejected(url: str, status: int, code: str) -> None:
    async with runweave() as client:
        checked = await client.post("/v1/agents/http/check", json={"url": url})
        registered = await client.post("/v1/agents/http", json={"url": url})
    assert (checked.status_code, checked.json()["detail"]["code"]) == (status, code)
    assert (registered.status_code, registered.json()["detail"]["code"]) == (status, code)


@pytest.mark.asyncio
async def test_built_in_python_and_startup_agents_cannot_be_removed() -> None:
    async with agent_server(upper) as url, runweave(http_agents=(url,)) as client:
        listed = {a["name"]: a for a in (await client.get("/v1/agents")).json()["agents"]}
        responses = [
            await client.delete(f"/v1/agents/{name}")
            for name in ("incident-triage", "echo", "remote-upper")
        ]
        missing = await client.delete("/v1/agents/nobody")
    assert listed["remote-upper"]["removable"] is False
    assert {r.status_code for r in responses} == {409}
    assert {r.json()["detail"]["code"] for r in responses} == {"AGENT_NOT_REMOVABLE"}
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_startup_fails_clearly_when_a_configured_http_agent_is_unreachable() -> None:
    app = create_app(Settings(http_agents=("http://127.0.0.1:1",)), agents=[])
    with pytest.raises(RuntimeError, match="AGENT_UNREACHABLE"):
        async with app.router.lifespan_context(app):
            pass


@pytest.mark.asyncio
async def test_managing_agents_requires_its_own_scope() -> None:
    settings = {
        "auth_mode": "test",
        "auth_test_identities": {
            "reader-token-0123456789": {
                "tenant": "t",
                "subject": "s",
                "scopes": ["runs:read", "runs:submit"],
            },
        },
    }
    headers = {"Authorization": "Bearer reader-token-0123456789"}
    async with runweave(**settings) as client:
        checked = await client.post(
            "/v1/agents/http/check", json={"url": "http://127.0.0.1:1"}, headers=headers
        )
        registered = await client.post(
            "/v1/agents/http", json={"url": "http://127.0.0.1:1"}, headers=headers
        )
        removed = await client.delete("/v1/agents/echo", headers=headers)
    assert {checked.status_code, registered.status_code, removed.status_code} == {403}


@pytest.mark.asyncio
async def test_registration_bodies_are_bounded() -> None:
    async with runweave() as client:
        response = await client.post(
            "/v1/agents/http",
            content=b'{"url": "' + b"a" * 20000 + b'"}',
            headers={"Content-Type": "application/json"},
        )
    assert response.status_code in {413, 422}
