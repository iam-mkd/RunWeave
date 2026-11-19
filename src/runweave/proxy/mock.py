"""Deterministic mock model: scripted replies from a fixture file, or a fixed default.

Fixture file (JSON), first matching rule wins:

    {"version": 1, "rules": [
      {"when": {"contains": "weather", "has_tools": true},
       "reply": {"tool_calls": [{"name": "lookup_weather", "arguments": {"city": "Paris"}}]}},
      {"when": {"last_role": "tool"}, "reply": {"text": "It is sunny."}}]}

`contains` matches the last user message (case-insensitive); `last_role` is "user" or
"tool" (the last message is a tool result); `api` is "chat", "responses" or "messages".
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

Api = Literal["chat", "responses", "messages"]
DEFAULT_TEXT = "This is a deterministic RunWeave mock response."
FIXTURE_MAX_BYTES = 1_048_576


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class FixtureWhen(_Strict):
    contains: str | None = Field(default=None, min_length=1, max_length=500)
    last_role: Literal["user", "tool"] | None = None
    has_tools: bool | None = None
    api: Api | None = None


class FixtureToolCall(_Strict):
    name: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,64}$")
    arguments: dict[str, Any] = Field(default_factory=dict)


class FixtureReply(_Strict):
    text: str | None = Field(default=None, max_length=100_000)
    tool_calls: list[FixtureToolCall] | None = Field(default=None, min_length=1, max_length=16)

    @model_validator(mode="after")
    def one_kind(self) -> Self:
        if (self.text is None) == (self.tool_calls is None):
            raise ValueError("A reply has either text or tool_calls")
        return self


class FixtureRule(_Strict):
    when: FixtureWhen = Field(default_factory=FixtureWhen)
    reply: FixtureReply


class Fixtures(_Strict):
    version: Literal[1]
    rules: list[FixtureRule] = Field(default_factory=list, max_length=500)


def load_fixtures(path: Path | None) -> Fixtures:
    if path is None:
        return Fixtures(version=1)
    try:
        data = path.read_bytes()
        if len(data) > FIXTURE_MAX_BYTES:
            raise ValueError("larger than 1 MiB")
        return Fixtures.model_validate_json(data)
    except (OSError, ValueError, ValidationError) as error:
        detail = f"{type(error).__name__}" if isinstance(error, ValidationError) else str(error)
        raise ValueError(f"Invalid proxy mock fixture file {path.name}: {detail}") from None


@dataclass(frozen=True)
class ToolCall:
    name: str
    arguments: str


@dataclass(frozen=True)
class ModelRequest:
    api: Api
    model: str
    last_role: str
    last_user_text: str
    tool_names: tuple[str, ...]
    stream: bool
    include_usage: bool
    max_tokens: int | None
    payload_bytes: int


@dataclass(frozen=True)
class ModelReply:
    text: str | None
    tool_calls: tuple[ToolCall, ...]
    input_tokens: int
    output_tokens: int
    truncated: bool


def _tokens(size: int) -> int:
    return max(1, -(-size // 4))


class MockModel:
    def __init__(self, fixtures: Fixtures) -> None:
        self._rules = fixtures.rules

    def _matches(self, when: FixtureWhen, request: ModelRequest) -> bool:
        return (
            (when.contains is None or when.contains.casefold() in request.last_user_text.casefold())
            and (when.last_role is None or when.last_role == request.last_role)
            and (when.has_tools is None or when.has_tools == bool(request.tool_names))
            and (when.api is None or when.api == request.api)
        )

    def reply(self, request: ModelRequest, *, output_limit: int) -> ModelReply:
        chosen = next((r.reply for r in self._rules if self._matches(r.when, request)), None)
        input_tokens = _tokens(request.payload_bytes)
        if chosen is not None and chosen.tool_calls is not None:
            calls = tuple(
                ToolCall(call.name, json.dumps(call.arguments, separators=(",", ":")))
                for call in chosen.tool_calls
            )
            size = sum(len(c.name) + len(c.arguments) for c in calls)
            return ModelReply(None, calls, input_tokens, min(output_limit, _tokens(size)), False)
        text = chosen.text if chosen is not None and chosen.text is not None else DEFAULT_TEXT
        truncated = _tokens(len(text.encode())) > output_limit
        if truncated:
            text = text[: output_limit * 4]
        return ModelReply(
            text, (), input_tokens, min(output_limit, _tokens(len(text.encode()))), truncated
        )


def broken_tool_call(request: ModelRequest, *, unknown: bool, output_limit: int) -> ModelReply:
    """A tool call with JSON cut off mid-value, or naming a tool the request did not declare."""
    name = request.tool_names[0] if request.tool_names else "tool"
    if unknown:
        name = "runweave_unknown_tool"
        while name in request.tool_names:
            name += "_x"
    arguments = '{"value":"runweave fault"}' if unknown else '{"value":"runwe'
    return ModelReply(
        None,
        (ToolCall(name, arguments),),
        _tokens(request.payload_bytes),
        min(output_limit, _tokens(len(name) + len(arguments))),
        False,
    )
