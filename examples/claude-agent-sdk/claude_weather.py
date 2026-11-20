"""A Claude Agent SDK agent that answers weather questions with one in-process MCP tool.

The SDK starts its bundled Claude Code CLI, which talks to RunWeave's LLM proxy
(`ctx.proxy`) instead of Anthropic. In mock mode the proxy answers from fixtures.json, so
the agent runs offline with no API key.
"""

import asyncio
import shutil
import tempfile
from typing import Any

from claude_agent_sdk import (
    ClaudeAgentOptions,
    ResultMessage,
    create_sdk_mcp_server,
    query,
    tool,
)
from pydantic import BaseModel, Field

from runweave.sdk import AgentUnavailable, RunContext, agent

FORECASTS = {"paris": "sunny, 21 °C", "london": "light rain, 14 °C", "tokyo": "cloudy, 18 °C"}
TOOL = "mcp__weather__get_weather"


class Question(BaseModel):
    question: str = Field(min_length=1, max_length=2000)


class Answer(BaseModel):
    answer: str
    turns: int


@agent(
    name="claude-weather",
    version="0.1.0",
    input=Question,
    output=Answer,
    description="Claude Agent SDK with an in-process MCP weather tool, through the proxy.",
    max_steps=10,
)
async def run(input: Question, ctx: RunContext) -> Answer:
    if ctx.proxy is None:
        raise AgentUnavailable()

    @tool("get_weather", "Current weather for a city", {"city": str})
    async def get_weather(arguments: dict[str, Any]) -> dict[str, Any]:
        city = str(arguments.get("city", ""))
        async with ctx.step("get_weather", detail=city):
            forecast = FORECASTS.get(city.casefold(), "unknown")
        return {"content": [{"type": "text", "text": f"{city}: {forecast}"}]}

    # A throwaway config folder keeps the CLI away from the developer's own Claude settings,
    # sessions and credentials.
    config_dir = await asyncio.to_thread(tempfile.mkdtemp, prefix="runweave-claude-")
    options = ClaudeAgentOptions(
        mcp_servers={"weather": create_sdk_mcp_server("weather", tools=[get_weather])},
        tools=[],  # no built-in tools (shell, files, web): only the MCP tool below
        allowed_tools=[TOOL],
        setting_sources=[],
        max_turns=4,
        env={
            **ctx.proxy.env(),
            "CLAUDE_CONFIG_DIR": config_dir,
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        },
    )
    try:
        async for message in query(prompt=input.question, options=options):
            if isinstance(message, ResultMessage):
                if message.is_error or message.result is None:
                    raise AgentUnavailable()
                return Answer(answer=message.result, turns=message.num_turns)
    finally:
        await asyncio.to_thread(shutil.rmtree, config_dir, ignore_errors=True)
    raise AgentUnavailable()
