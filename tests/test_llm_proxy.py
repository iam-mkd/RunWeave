"""The local LLM proxy in mock mode, driven by the official SDKs and PydanticAI.

In-process agents call the proxy with the run's own token (ctx.proxy). The SDK clients use
an ASGI transport into the same app, so nothing leaves the process.
"""

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import anthropic
import httpx
import httpx2
import openai
import pytest
from fastapi import FastAPI
from pydantic import BaseModel, Field

from runweave.agents.catalog import PythonAgent
from runweave.app import create_app
from runweave.core.settings import Settings
from runweave.sdk import RunContext, agent

SECRET_PROMPT = "prompt-marker-7f3a do not log me"
Scenario = Callable[[RunContext], Awaitable[dict[str, Any]]]
SCENARIOS: dict[str, Scenario] = {}
APP: dict[str, FastAPI] = {}


def transport() -> httpx.AsyncClient:
    """The test's own client into the app (RunWeave's API uses httpx)."""
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=APP["app"]), base_url="http://test")


def openai_client(ctx: RunContext) -> openai.AsyncOpenAI:
    assert ctx.proxy is not None
    return openai.AsyncOpenAI(
        api_key=ctx.proxy.token,
        base_url=ctx.proxy.openai_base_url,
        # The OpenAI and Anthropic SDKs are built on httpx2.
        http_client=httpx2.AsyncClient(transport=httpx2.ASGITransport(app=APP["app"])),
        max_retries=0,
    )


def anthropic_client(ctx: RunContext) -> anthropic.AsyncAnthropic:
    assert ctx.proxy is not None
    return anthropic.AsyncAnthropic(
        api_key=ctx.proxy.token,
        base_url=ctx.proxy.anthropic_base_url,
        # Anthropic's SDK is built on httpx2, so it needs httpx2's ASGI transport.
        http_client=httpx2.AsyncClient(transport=httpx2.ASGITransport(app=APP["app"])),
        max_retries=0,
    )


def scenario(function: Scenario) -> Scenario:
    SCENARIOS[function.__name__] = function
    return function


class Script(BaseModel):
    scenario: str = Field(max_length=60)


class Outcome(BaseModel):
    data: dict[str, Any]


@agent(name="proxy-user", version="1", input=Script, output=Outcome, max_steps=50)
async def proxy_user(input: Script, ctx: RunContext) -> Outcome:
    return Outcome(data=await SCENARIOS[input.scenario](ctx))


@scenario
async def chat_text(ctx: RunContext) -> dict[str, Any]:
    client = openai_client(ctx)
    first = await client.chat.completions.create(
        model="gpt-anything", messages=[{"role": "user", "content": SECRET_PROMPT}]
    )
    second = await client.chat.completions.create(
        model="gpt-anything", messages=[{"role": "user", "content": SECRET_PROMPT}]
    )
    return {
        "text": first.choices[0].message.content,
        "same": first.choices[0].message.content == second.choices[0].message.content,
        "finish": first.choices[0].finish_reason,
        "usage": first.usage.model_dump() if first.usage else None,
        "object": first.object,
    }


@scenario
async def chat_stream(ctx: RunContext) -> dict[str, Any]:
    client = openai_client(ctx)
    stream = await client.chat.completions.create(
        model="gpt-anything",
        messages=[{"role": "user", "content": "hello"}],
        stream=True,
        stream_options={"include_usage": True},
    )
    parts: list[str] = []
    usage = None
    chunks = 0
    async for chunk in stream:
        chunks += 1
        if chunk.choices and chunk.choices[0].delta.content:
            parts.append(chunk.choices[0].delta.content)
        if chunk.usage:
            usage = chunk.usage.model_dump()
    return {"text": "".join(parts), "chunks": chunks, "usage": usage}


@scenario
async def chat_tools(ctx: RunContext) -> dict[str, Any]:
    client = openai_client(ctx)
    tools: Any = [
        {
            "type": "function",
            "function": {
                "name": "lookup_weather",
                "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
            },
        }
    ]
    messages: Any = [{"role": "user", "content": "weather in Paris?"}]
    first = await client.chat.completions.create(model="m", messages=messages, tools=tools)
    call = first.choices[0].message.tool_calls[0]  # type: ignore[index]
    messages += [
        first.choices[0].message.model_dump(exclude_none=True),
        {"role": "tool", "tool_call_id": call.id, "content": "sunny"},
    ]
    second = await client.chat.completions.create(model="m", messages=messages, tools=tools)
    streamed = await client.chat.completions.create(
        model="m",
        messages=[{"role": "user", "content": "weather in Paris?"}],
        tools=tools,
        stream=True,
    )
    pieces: dict[int, dict[str, str]] = {}
    async for chunk in streamed:
        for delta in chunk.choices[0].delta.tool_calls or [] if chunk.choices else []:
            entry = pieces.setdefault(delta.index, {"name": "", "arguments": ""})
            if delta.function and delta.function.name:
                entry["name"] += delta.function.name
            if delta.function and delta.function.arguments:
                entry["arguments"] += delta.function.arguments
    return {
        "finish": first.choices[0].finish_reason,
        "name": call.function.name,  # type: ignore[union-attr]
        "arguments": json.loads(call.function.arguments),  # type: ignore[union-attr]
        "final": second.choices[0].message.content,
        "streamed": [
            {"name": p["name"], "arguments": json.loads(p["arguments"])} for p in pieces.values()
        ],
    }


@scenario
async def responses_api(ctx: RunContext) -> dict[str, Any]:
    client = openai_client(ctx)
    plain = await client.responses.create(model="m", input="hello there")
    tools: Any = [
        {
            "type": "function",
            "name": "lookup_weather",
            "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
        }
    ]
    called = await client.responses.create(model="m", input="weather in Paris?", tools=tools)
    calls = [item for item in called.output if item.type == "function_call"]
    events: list[str] = []
    text: list[str] = []
    stream = await client.responses.create(model="m", input="hello there", stream=True)
    async for event in stream:
        events.append(event.type)
        if event.type == "response.output_text.delta":
            text.append(event.delta)
    return {
        "text": plain.output_text,
        "usage": plain.usage.model_dump() if plain.usage else None,
        "call": {"name": calls[0].name, "arguments": json.loads(calls[0].arguments)},
        "stream_text": "".join(text),
        "events": events,
    }


@scenario
async def anthropic_messages(ctx: RunContext) -> dict[str, Any]:
    client = anthropic_client(ctx)
    plain = await client.messages.create(
        model="claude-anything", max_tokens=200, messages=[{"role": "user", "content": "hello"}]
    )
    tools: Any = [
        {
            "name": "lookup_weather",
            "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}},
        }
    ]
    called = await client.messages.create(
        model="claude-anything",
        max_tokens=200,
        tools=tools,
        messages=[{"role": "user", "content": "weather in Paris?"}],
    )
    tool_use = next(block for block in called.content if block.type == "tool_use")
    async with client.messages.stream(
        model="claude-anything", max_tokens=200, messages=[{"role": "user", "content": "hello"}]
    ) as stream:
        streamed_text = "".join([text async for text in stream.text_stream])
        final = await stream.get_final_message()
    async with client.messages.stream(
        model="claude-anything",
        max_tokens=200,
        tools=tools,
        messages=[{"role": "user", "content": "weather in Paris?"}],
    ) as stream:
        streamed_tool = await stream.get_final_message()
    streamed_use = next(block for block in streamed_tool.content if block.type == "tool_use")
    return {
        "text": plain.content[0].text,  # type: ignore[union-attr]
        "stop": plain.stop_reason,
        "usage": plain.usage.model_dump(include={"input_tokens", "output_tokens"}),
        "tool": {"name": tool_use.name, "input": tool_use.input, "stop": called.stop_reason},
        "stream_text": streamed_text,
        "stream_usage": final.usage.output_tokens,
        "stream_tool": {"name": streamed_use.name, "input": streamed_use.input},
    }


@scenario
async def pydantic_ai(ctx: RunContext) -> dict[str, Any]:
    from pydantic_ai import Agent
    from pydantic_ai.models.anthropic import AnthropicModel
    from pydantic_ai.models.openai import OpenAIChatModel
    from pydantic_ai.providers.anthropic import AnthropicProvider
    from pydantic_ai.providers.openai import OpenAIProvider

    chat = Agent(
        OpenAIChatModel("gpt-anything", provider=OpenAIProvider(openai_client=openai_client(ctx)))
    )
    claude = Agent(
        AnthropicModel(
            "claude-anything", provider=AnthropicProvider(anthropic_client=anthropic_client(ctx))
        )
    )
    result = await chat.run("hello")
    async with chat.run_stream("hello") as streamed:
        streamed_text = await streamed.get_output()
    claude_result = await claude.run("hello")
    return {"chat": result.output, "stream": streamed_text, "claude": claude_result.output}


@scenario
async def budget(ctx: RunContext) -> dict[str, Any]:
    client = openai_client(ctx)
    replies = []
    for _ in range(3):
        try:
            response = await client.chat.completions.create(
                model="m", messages=[{"role": "user", "content": "hi"}]
            )
            replies.append(response.choices[0].message.content)
        except openai.APIStatusError as error:
            replies.append(
                f"{error.status_code}:{error.body['code'] if isinstance(error.body, dict) else ''}"
            )
    return {"replies": replies}


@scenario
async def leak_token(ctx: RunContext) -> dict[str, Any]:
    assert ctx.proxy is not None
    return {"token": ctx.proxy.token, "env": ctx.proxy.env()}


FIXTURES = {
    "version": 1,
    "rules": [
        {"when": {"last_role": "tool"}, "reply": {"text": "It is sunny in Paris."}},
        {
            "when": {"contains": "weather", "has_tools": True},
            "reply": {"tool_calls": [{"name": "lookup_weather", "arguments": {"city": "Paris"}}]},
        },
        {"when": {"contains": "hello"}, "reply": {"text": "Hello from the RunWeave mock."}},
    ],
}


@asynccontextmanager
async def runweave(tmp_path: Path, **settings: Any) -> AsyncIterator[httpx.AsyncClient]:
    fixtures = tmp_path / "fixtures.json"
    fixtures.write_text(json.dumps(FIXTURES), encoding="utf-8")
    config = Settings(proxy_mock_fixtures=fixtures, proxy_public_url="http://test", **settings)
    app = create_app(
        config, agents=[PythonAgent.from_function(proxy_user, target="tests:proxy_user")]
    )
    APP["app"] = app
    async with app.router.lifespan_context(app), transport() as client:
        yield client


async def run(client: httpx.AsyncClient, name: str) -> dict[str, Any]:
    response = await client.post(
        "/v1/runs", json={"agent": "proxy-user", "input": {"scenario": name}}
    )
    assert response.status_code == 202, response.text
    url = response.json()["status_url"]
    async with asyncio.timeout(20):
        while True:
            body: dict[str, Any] = (await client.get(url)).json()
            if body["status"] in {"SUCCEEDED", "FAILED", "TIMED_OUT", "CANCELLED"}:
                return body
            await asyncio.sleep(0.02)


@pytest.mark.asyncio
async def test_chat_completions_are_deterministic_and_metered(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    async with runweave(tmp_path) as client:
        result = await run(client, "chat_text")
    assert result["status"] == "SUCCEEDED", result
    data = result["result"]["data"]
    assert data["object"] == "chat.completion"
    assert data["same"] is True and data["finish"] == "stop"
    assert data["text"] == "This is a deterministic RunWeave mock response."
    assert data["usage"]["prompt_tokens"] > 0 and data["usage"]["completion_tokens"] > 0
    usage = result["usage"]
    assert usage["requests"] == 2
    assert usage["input_tokens"] == 2 * data["usage"]["prompt_tokens"]
    calls = [step for step in result["steps"] if step["title"] == "Model call"]
    assert len(calls) == 2
    assert calls[0]["detail"].startswith("chat.completions · mock")
    # Prompts and completions are never logged or stored in the timeline.
    assert SECRET_PROMPT not in json.dumps(result["steps"])
    assert SECRET_PROMPT not in caplog.text and data["text"] not in caplog.text


@pytest.mark.asyncio
async def test_chat_streaming_returns_chunks_and_usage(tmp_path: Path) -> None:
    async with runweave(tmp_path) as client:
        result = await run(client, "chat_stream")
    data = result["result"]["data"]
    assert data["text"] == "Hello from the RunWeave mock."
    assert data["chunks"] > 3
    assert data["usage"]["completion_tokens"] > 0
    assert result["usage"]["requests"] == 1


@pytest.mark.asyncio
async def test_fixture_tool_calls_round_trip_and_stream(tmp_path: Path) -> None:
    async with runweave(tmp_path) as client:
        result = await run(client, "chat_tools")
    data = result["result"]["data"]
    assert (data["finish"], data["name"], data["arguments"]) == (
        "tool_calls",
        "lookup_weather",
        {"city": "Paris"},
    )
    assert data["final"] == "It is sunny in Paris."
    assert data["streamed"] == [{"name": "lookup_weather", "arguments": {"city": "Paris"}}]


@pytest.mark.asyncio
async def test_responses_api_text_tools_and_streaming(tmp_path: Path) -> None:
    async with runweave(tmp_path) as client:
        result = await run(client, "responses_api")
    data = result["result"]["data"]
    assert data["text"] == "Hello from the RunWeave mock."
    assert data["usage"]["input_tokens"] > 0
    assert data["call"] == {"name": "lookup_weather", "arguments": {"city": "Paris"}}
    assert data["stream_text"] == "Hello from the RunWeave mock."
    assert data["events"][0] == "response.created" and data["events"][-1] == "response.completed"
    assert "response.output_text.delta" in data["events"]


@pytest.mark.asyncio
async def test_anthropic_messages_text_tools_and_streaming(tmp_path: Path) -> None:
    async with runweave(tmp_path) as client:
        result = await run(client, "anthropic_messages")
    assert result["status"] == "SUCCEEDED", result
    data = result["result"]["data"]
    assert (data["text"], data["stop"]) == ("Hello from the RunWeave mock.", "end_turn")
    assert data["usage"]["input_tokens"] > 0
    assert data["tool"] == {
        "name": "lookup_weather",
        "input": {"city": "Paris"},
        "stop": "tool_use",
    }
    assert data["stream_text"] == "Hello from the RunWeave mock." and data["stream_usage"] > 0
    assert data["stream_tool"] == {"name": "lookup_weather", "input": {"city": "Paris"}}
    titles = {
        step["detail"].split(" · ")[0] for step in result["steps"] if step["title"] == "Model call"
    }
    assert titles == {"messages"}


@pytest.mark.asyncio
async def test_pydantic_ai_runs_unmodified_through_the_proxy(tmp_path: Path) -> None:
    async with runweave(tmp_path) as client:
        result = await run(client, "pydantic_ai")
    assert result["status"] == "SUCCEEDED", result
    assert result["result"]["data"] == {
        "chat": "Hello from the RunWeave mock.",
        "stream": "Hello from the RunWeave mock.",
        "claude": "Hello from the RunWeave mock.",
    }


@pytest.mark.asyncio
async def test_budgets_stop_requests_before_they_are_answered(tmp_path: Path) -> None:
    async with runweave(tmp_path, agent_request_limit=2) as client:
        result = await run(client, "budget")
    replies = result["result"]["data"]["replies"]
    assert replies[:2] == ["This is a deterministic RunWeave mock response."] * 2
    assert replies[2] == "402:RUN_BUDGET_EXCEEDED"
    assert result["usage"]["requests"] == 2
    calls = [s for s in result["steps"] if s["title"] == "Model call"]
    assert [s["status"] for s in calls] == ["succeeded", "succeeded", "failed"]
    assert calls[2]["detail"].endswith("RUN_BUDGET_EXCEEDED")


@pytest.mark.asyncio
async def test_tokens_are_bound_to_one_running_run(tmp_path: Path) -> None:
    async with runweave(tmp_path) as client:
        result = await run(client, "leak_token")
        token = result["result"]["data"]["token"]
        env = result["result"]["data"]["env"]
        body = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
        missing = await client.post("/llm/v1/chat/completions", json=body)
        wrong = await client.post(
            "/llm/v1/chat/completions", json=body, headers={"Authorization": "Bearer rwp_wrong"}
        )
        expired = await client.post(
            "/llm/v1/chat/completions", json=body, headers={"Authorization": f"Bearer {token}"}
        )
        claude = await client.post(
            "/llm/v1/messages",
            json={"model": "c", "max_tokens": 5, "messages": body["messages"]},
            headers={"x-api-key": token},
        )
    assert token.startswith("rwp_") and len(token) > 40
    assert env == {
        "OPENAI_BASE_URL": "http://test/llm/v1",
        "OPENAI_API_KEY": token,
        "ANTHROPIC_BASE_URL": "http://test/llm",
        "ANTHROPIC_API_KEY": token,
    }
    for response in (missing, wrong, expired):
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "PROXY_TOKEN_INVALID"
        assert response.json()["error"]["type"] == "invalid_request_error"
    assert claude.status_code == 401
    assert claude.json() == {
        "type": "error",
        "error": {"type": "authentication_error", "message": claude.json()["error"]["message"]},
    }
    assert token not in json.dumps(result["steps"])


@pytest.mark.asyncio
async def test_proxy_requests_are_size_limited(tmp_path: Path) -> None:
    async with runweave(tmp_path, proxy_request_max_bytes=4096) as client:
        response = await client.post(
            "/llm/v1/chat/completions",
            content=b"x" * 10000,
            headers={"Content-Type": "application/json", "Authorization": "Bearer rwp_x"},
        )
    assert response.status_code == 413


def test_invalid_fixture_files_fail_at_startup(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text('{"version": 1, "rules": [{"when": {}, "reply": {}}]}', encoding="utf-8")
    with pytest.raises(ValueError, match="fixture"):
        create_app(Settings(proxy_mock_fixtures=bad), agents=[])
