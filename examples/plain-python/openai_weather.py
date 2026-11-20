"""A plain Python agent: the `openai` client, Chat Completions and a small tool loop.

The client talks to RunWeave's LLM proxy (`ctx.proxy`). In mock mode the proxy answers
from fixtures.json, so the agent runs offline with no API key.
"""

import json
from typing import Any

from openai import AsyncOpenAI
from openai.types.chat import ChatCompletionMessageParam, ChatCompletionToolParam
from pydantic import BaseModel, Field

from runweave.sdk import AgentError, AgentUnavailable, RunContext, agent

FORECASTS = {"paris": "sunny, 21 °C", "london": "light rain, 14 °C", "tokyo": "cloudy, 18 °C"}
MAX_TURNS = 4
TOOLS: list[ChatCompletionToolParam] = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Current weather for a city",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
                "additionalProperties": False,
            },
        },
    }
]


class Question(BaseModel):
    question: str = Field(min_length=1, max_length=2000)


class Answer(BaseModel):
    answer: str
    turns: int


class TooManyTurns(AgentError):
    code = "AGENT_TOO_MANY_TURNS"


def get_weather(city: str) -> str:
    return f"{city}: {FORECASTS.get(city.casefold(), 'unknown')}"


def _call_tool(name: str, raw_arguments: str) -> str:
    # Model output is untrusted: only declared tools run, with validated arguments.
    try:
        arguments: Any = json.loads(raw_arguments)
    except json.JSONDecodeError:
        return "error: arguments are not valid JSON"
    if name != "get_weather" or not isinstance(arguments, dict):
        return f"error: unknown tool {name[:64]}"
    city = arguments.get("city")
    if not isinstance(city, str) or not 0 < len(city) <= 100:
        return "error: city must be a non-empty string"
    return get_weather(city)


@agent(
    name="plain-python-weather",
    version="0.1.0",
    input=Question,
    output=Answer,
    description="The openai client and a Chat Completions tool loop, through the proxy.",
    max_steps=10,
)
async def run(input: Question, ctx: RunContext) -> Answer:
    if ctx.proxy is None:
        raise AgentUnavailable()
    messages: list[ChatCompletionMessageParam] = [
        {"role": "system", "content": "Answer weather questions with the get_weather tool."},
        {"role": "user", "content": input.question},
    ]
    async with AsyncOpenAI(
        api_key=ctx.proxy.token, base_url=ctx.proxy.openai_base_url, max_retries=2
    ) as client:
        for turn in range(1, MAX_TURNS + 1):
            completion = await client.chat.completions.create(
                model="gpt-4.1-mini", messages=messages, tools=TOOLS, max_tokens=512
            )
            message = completion.choices[0].message
            calls = [call for call in message.tool_calls or [] if call.type == "function"]
            if not calls:
                return Answer(answer=message.content or "", turns=turn)
            messages.append(
                {
                    "role": "assistant",
                    "content": message.content,
                    "tool_calls": [
                        {
                            "id": call.id,
                            "type": "function",
                            "function": {
                                "name": call.function.name,
                                "arguments": call.function.arguments,
                            },
                        }
                        for call in calls
                    ],
                }
            )
            for call in calls:
                async with ctx.step(call.function.name[:64], detail=call.function.arguments):
                    result = _call_tool(call.function.name, call.function.arguments)
                messages.append({"role": "tool", "tool_call_id": call.id, "content": result})
    raise TooManyTurns()
