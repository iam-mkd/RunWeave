import json
from typing import Literal, cast

from pydantic import BaseModel

LiveProvider = Literal["groq", "openai", "anthropic", "xai", "gemini"]
Provider = Literal["fake", LiveProvider]

ENDPOINTS: dict[LiveProvider, str] = {
    "groq": "https://api.groq.com/openai/v1/chat/completions",
    "openai": "https://api.openai.com/v1/chat/completions",
    "anthropic": "https://api.anthropic.com/v1/messages",
    "xai": "https://api.x.ai/v1/chat/completions",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
}


def request_data(
    provider: LiveProvider, payload: dict[str, object], key: str
) -> tuple[dict[str, object], dict[str, str]]:
    if provider == "anthropic":
        return anthropic_payload(payload), {
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
        }
    if provider in ("xai", "gemini"):
        payload["max_tokens"] = payload.pop("max_completion_tokens")
    if provider == "gemini":
        payload.pop("parallel_tool_calls")
    return payload, {"Authorization": f"Bearer {key}"}


def anthropic_payload(payload: dict[str, object]) -> dict[str, object]:
    original = cast(list[dict[str, object]], payload["messages"])
    tools = cast(list[dict[str, object]], payload["tools"])
    messages: list[dict[str, object]] = []
    for msg in original[1:]:
        if msg["role"] == "assistant":
            calls = cast(list[dict[str, object]], msg["tool_calls"])
            call = calls[0]
            fn = cast(dict[str, str], call["function"])
            messages.append(
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": call["id"],
                            "name": fn["name"],
                            "input": json.loads(fn["arguments"]),
                        }
                    ],
                }
            )
        elif msg["role"] == "tool":
            messages.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": msg["tool_call_id"],
                            "content": msg["content"],
                        }
                    ],
                }
            )
        else:
            messages.append(msg)
    definitions = [cast(dict[str, object], tool["function"]) for tool in tools]
    return {
        "model": payload["model"],
        "system": original[0]["content"],
        "messages": messages,
        "max_tokens": payload["max_completion_tokens"],
        "tool_choice": {"type": "any", "disable_parallel_tool_use": True},
        "tools": [
            {
                "name": tool["name"],
                "description": tool["description"],
                "input_schema": tool["parameters"],
            }
            for tool in definitions
        ],
    }


class AnthropicUsage(BaseModel):
    input_tokens: int
    output_tokens: int
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0


class AnthropicEnvelope(BaseModel):
    stop_reason: str
    usage: AnthropicUsage
    content: list[dict[str, object]]


class AnthropicCall(BaseModel):
    id: str
    name: str
    input: dict[str, object]
