"""Runtime settings: providers, budgets and fault profiles, and keys that never leave RunWeave."""

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import keyring
import pytest
from keyring.backend import KeyringBackend
from pydantic import BaseModel, SecretStr

from runweave.agents.catalog import PythonAgent
from runweave.app import create_app
from runweave.core.settings import Settings
from runweave.keystore import KeyringStore, MemoryKeyStore
from runweave.sdk import RunContext, agent

SAVED_KEY = "sk-saved-in-store-4b9c8d7e6f5a"
APP: dict[str, Any] = {}


class Prompt(BaseModel):
    calls: int = 1
    tools: bool = False


class Results(BaseModel):
    statuses: list[int]


@agent(name="caller", version="1", input=Prompt, output=Results, max_steps=20)
async def caller(input: Prompt, ctx: RunContext) -> Results:
    assert ctx.proxy is not None
    statuses = []
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=APP["app"]), base_url="http://test"
    ) as http:
        for _ in range(input.calls):
            response = await http.post(
                "/llm/v1/chat/completions",
                json={"model": "gpt-test", "max_tokens": 20,
                      "messages": [{"role": "user", "content": "hello"}]},
                headers={"Authorization": f"Bearer {ctx.proxy.token}"},
            )
            statuses.append(response.status_code)
    return Results(statuses=statuses)


class Upstream:
    def __init__(self) -> None:
        self.keys: list[str] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.keys.append(request.headers.get("authorization", ""))
        return httpx.Response(200, json={
            "id": "c", "object": "chat.completion", "created": 1, "model": "gpt-test",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "real"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
        })


@asynccontextmanager
async def workspace(
    upstream: Upstream | None = None, **settings: Any
) -> AsyncIterator[httpx.AsyncClient]:
    config = Settings(key_store=settings.pop("key_store", "memory"), **settings)
    app = create_app(
        config,
        agents=[PythonAgent.from_function(caller, target="tests.test_settings_api:caller")],
        upstream_transport=httpx.MockTransport(upstream) if upstream else None,
    )
    APP["app"] = app
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        yield client


async def run(client: httpx.AsyncClient, **payload: Any) -> dict[str, Any]:
    response = await client.post("/v1/runs", json={"agent": "caller", "input": payload})
    assert response.status_code == 202, response.text
    async with asyncio.timeout(10):
        while True:
            body: dict[str, Any] = (await client.get(response.json()["status_url"])).json()
            if body["status"] in {"SUCCEEDED", "FAILED", "TIMED_OUT", "CANCELLED"}:
                return body
            await asyncio.sleep(0.02)


PASSTHROUGH = {
    "proxy_mode": "passthrough",
    "proxy_models": ["gpt-test"],
    "model_input_usd_per_million": "1.5",
    "model_output_usd_per_million": "6",
    "model_price_version": "test-2026-11",
}


@pytest.mark.asyncio
async def test_settings_view_describes_providers_budgets_and_faults() -> None:
    async with workspace(proxy_openai_api_key=SecretStr("sk-env-only-1234567890")) as client:
        view = (await client.get("/v1/settings")).json()
    assert view["proxy"]["mode"] == "mock"
    assert view["proxy"]["openai"]["key"] == "environment"
    assert view["proxy"]["anthropic"]["key"] == "none"
    assert view["proxy"]["openai"]["base_url"] == "https://api.openai.com/v1"
    assert view["key_store"] == {"available": True, "backend": "memory"}
    assert view["budgets"]["agent_request_limit"] == 20
    names = [p["name"] for p in view["faults"]["profiles"]]
    assert {"flaky", "chaos"} <= set(names)
    assert view["faults"]["profile"] == "" and view["faults"]["seed"] == 0
    assert "sk-env-only" not in json.dumps(view)


@pytest.mark.asyncio
async def test_budget_changes_apply_to_runs_started_afterwards() -> None:
    async with workspace() as client:
        before = await run(client, calls=2)
        changed = await client.patch("/v1/settings", json={"budgets": {"agent_request_limit": 1}})
        after = await run(client, calls=2)
    assert changed.status_code == 200
    assert changed.json()["budgets"]["agent_request_limit"] == 1
    assert before["result"]["statuses"] == [200, 200]
    assert after["result"]["statuses"] == [200, 402]


@pytest.mark.asyncio
async def test_fault_profile_changes_apply_immediately() -> None:
    async with workspace() as client:
        changed = await client.patch(
            "/v1/settings", json={"faults": {"profile": "outage", "seed": 5}}
        )
        result = await run(client, calls=1)
        cleared = await client.patch("/v1/settings", json={"faults": {"profile": ""}})
        healthy = await run(client, calls=1)
    assert changed.json()["faults"] == {**changed.json()["faults"], "profile": "outage", "seed": 5}
    assert result["result"]["statuses"] == [503]
    steps = [s for s in result["steps"] if s["kind"] == "model_call"]
    assert steps[0]["fault"] == "server_error" and steps[0]["status"] == "failed"
    assert cleared.status_code == 200 and healthy["result"]["statuses"] == [200]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("patch", "field"),
    [
        ({"faults": {"profile": "nope"}}, "profile"),
        ({"budgets": {"agent_request_limit": 0}}, "agent_request_limit"),
        ({"proxy": {"mode": "passthrough"}}, "mode"),
        ({"proxy": {"mode": "bogus"}}, "mode"),
        ({"unknown": 1}, "unknown"),
    ],
)
async def test_invalid_changes_are_rejected_and_nothing_changes(
    patch: dict[str, Any], field: str
) -> None:
    async with workspace() as client:
        response = await client.patch("/v1/settings", json=patch)
        view = (await client.get("/v1/settings")).json()
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "INVALID_SETTINGS"
    assert view["proxy"]["mode"] == "mock" and view["faults"]["profile"] == ""
    assert view["budgets"]["agent_request_limit"] == 20


@pytest.mark.asyncio
async def test_saved_keys_are_used_for_passthrough_and_never_returned(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    upstream = Upstream()
    async with workspace(upstream) as client:
        saved = await client.put("/v1/settings/keys/openai", json={"key": SAVED_KEY})
        switched = await client.patch(
            "/v1/settings",
            json={"proxy": {"mode": "passthrough", "models": ["gpt-test"]},
                  "budgets": {k: v for k, v in PASSTHROUGH.items() if k.startswith("model_")}},
        )
        result = await run(client, calls=1)
        view = await client.get("/v1/settings")
        removed = await client.delete("/v1/settings/keys/openai")
        after = await run(client, calls=1)
        responses = [saved, switched, view, removed]
    assert saved.status_code == 204 and removed.status_code == 204
    assert switched.status_code == 200, switched.text
    assert result["result"]["statuses"] == [200]
    assert upstream.keys == [f"Bearer {SAVED_KEY}"]
    assert view.json()["proxy"]["openai"]["key"] == "credential_store"
    assert after["result"]["statuses"] == [400]  # PROVIDER_NOT_CONFIGURED once removed
    for response in responses:
        assert SAVED_KEY not in response.text and SAVED_KEY not in str(response.headers)
    assert SAVED_KEY not in json.dumps(result) + json.dumps(after)
    assert SAVED_KEY not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["short", "has space inside 1234", "x" * 600, "ключ-1234567890"])
async def test_invalid_keys_are_rejected_without_echoing_them(key: str) -> None:
    async with workspace() as client:
        response = await client.put("/v1/settings/keys/openai", json={"key": key})
    assert response.status_code == 422
    assert key not in response.text


@pytest.mark.asyncio
async def test_keys_set_by_the_environment_or_without_a_store_cannot_be_saved() -> None:
    async with workspace(proxy_anthropic_api_key=SecretStr("sk-ant-env-1234567890")) as client:
        env = await client.put("/v1/settings/keys/anthropic", json={"key": SAVED_KEY})
        unknown = await client.put("/v1/settings/keys/gemini", json={"key": SAVED_KEY})
    async with workspace(key_store="none") as client:
        none = await client.put("/v1/settings/keys/openai", json={"key": SAVED_KEY})
        view = (await client.get("/v1/settings")).json()
    assert (env.status_code, env.json()["detail"]["code"]) == (409, "KEY_SET_BY_ENVIRONMENT")
    assert unknown.status_code == 404
    assert (none.status_code, none.json()["detail"]["code"]) == (409, "KEY_STORE_UNAVAILABLE")
    assert view["key_store"]["available"] is False


@pytest.mark.asyncio
async def test_settings_need_their_own_scope() -> None:
    identities = {"reader-token-0123456789": {
        "tenant": "t", "subject": "s", "scopes": ["runs:read", "runs:submit"],
    }}
    headers = {"Authorization": "Bearer reader-token-0123456789"}
    async with workspace(auth_mode="test", auth_test_identities=identities) as client:
        responses = [
            await client.get("/v1/settings", headers=headers),
            await client.patch("/v1/settings", json={}, headers=headers),
            await client.put("/v1/settings/keys/openai", json={"key": SAVED_KEY}, headers=headers),
            await client.delete("/v1/settings/keys/openai", headers=headers),
        ]
    assert {r.status_code for r in responses} == {403}


class FakeKeyring(KeyringBackend):
    priority = 1  # type: ignore[assignment]

    def __init__(self) -> None:
        super().__init__()
        self.values: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.values.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.values[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        self.values.pop((service, username))


@pytest.mark.asyncio
async def test_keyring_store_uses_the_os_credential_store_off_the_event_loop() -> None:
    previous = keyring.get_keyring()
    fake = FakeKeyring()
    keyring.set_keyring(fake)
    try:
        store = KeyringStore()
        assert store.available and store.backend == "FakeKeyring"
        await store.set("openai", SAVED_KEY)
        assert fake.values == {("RunWeave", "proxy-openai"): SAVED_KEY}
        assert await store.get("openai") == SAVED_KEY
        await store.delete("openai")
        await store.delete("openai")  # Deleting a missing key is not an error.
        assert await store.get("openai") is None
        from keyring.backends.fail import Keyring as FailKeyring

        keyring.set_keyring(FailKeyring())
        assert KeyringStore().available is False
    finally:
        keyring.set_keyring(previous)


@pytest.mark.asyncio
async def test_memory_store_round_trips() -> None:
    store = MemoryKeyStore()
    await store.set("anthropic", SAVED_KEY)
    assert await store.get("anthropic") == SAVED_KEY
    await store.delete("anthropic")
    assert await store.get("anthropic") is None
