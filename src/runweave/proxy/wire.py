"""Request parsing and response rendering for the three proxied wire formats.

Only what the mock needs is read from requests (roles, the last user text, tool names,
limits); bodies are never stored or logged.
"""

import json
import re
import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from runweave.proxy.mock import Api, ModelReply, ModelRequest

_PIECES = re.compile(r"\S+\s*|\s+")


class WireError(ValueError):
    """The request body is not a valid request for this API."""


@dataclass(frozen=True)
class Ids:
    response: str
    message: str
    calls: tuple[str, ...]


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            str(part.get("text", ""))
            for part in content
            if isinstance(part, dict) and part.get("type") in {"text", "input_text", "output_text"}
        )
    return ""


def _positive(value: Any) -> int | None:
    if value is None:
        return None
    if type(value) is not int or value < 1:
        raise WireError("Token limits must be positive integers")
    return value


def parse(api: Api, body: Any, size: int) -> ModelRequest:
    if not isinstance(body, dict) or not isinstance(body.get("model"), str):
        raise WireError("A JSON object with a model is required")
    tools = body.get("tools") or []
    if not isinstance(tools, list):
        raise WireError("tools must be a list")
    if api == "chat":
        messages = body.get("messages")
        if not isinstance(messages, list) or not messages:
            raise WireError("messages must be a non-empty list")
        roles = [m.get("role") for m in messages if isinstance(m, dict)]
        last_role = "tool" if roles and roles[-1] in {"tool", "function"} else "user"
        users = [
            _text(m.get("content"))
            for m in messages
            if isinstance(m, dict) and m.get("role") == "user"
        ]
        names = tuple(
            str((t.get("function") or {}).get("name", "")) for t in tools if isinstance(t, dict)
        )
        limit = _positive(body.get("max_completion_tokens", body.get("max_tokens")))
        options = body.get("stream_options") or {}
        include_usage = isinstance(options, dict) and options.get("include_usage") is True
    elif api == "responses":
        items = body.get("input")
        if isinstance(items, str):
            items = [{"role": "user", "content": items}]
        if not isinstance(items, list) or not items:
            raise WireError("input must be a string or a non-empty list")
        last = items[-1] if isinstance(items[-1], dict) else {}
        last_role = "tool" if last.get("type") == "function_call_output" else "user"
        users = [
            _text(i.get("content"))
            for i in items
            if isinstance(i, dict) and i.get("role") == "user"
        ]
        names = tuple(str(t.get("name", "")) for t in tools if isinstance(t, dict))
        limit = _positive(body.get("max_output_tokens"))
        include_usage = True
    else:
        messages = body.get("messages")
        if not isinstance(messages, list) or not messages:
            raise WireError("messages must be a non-empty list")
        limit = _positive(body.get("max_tokens"))
        if limit is None:
            raise WireError("max_tokens is required")
        last = messages[-1] if isinstance(messages[-1], dict) else {}
        content = last.get("content")
        tool_result = isinstance(content, list) and any(
            isinstance(b, dict) and b.get("type") == "tool_result" for b in content
        )
        last_role = "tool" if tool_result else "user"
        users = [
            _text(m.get("content"))
            for m in messages
            if isinstance(m, dict) and m.get("role") == "user" and _text(m.get("content"))
        ]
        names = tuple(str(t.get("name", "")) for t in tools if isinstance(t, dict))
        include_usage = True
    return ModelRequest(
        api=api,
        model=body["model"][:200],
        last_role=last_role,
        last_user_text=users[-1] if users else "",
        tool_names=tuple(n for n in names if n),
        stream=body.get("stream") is True,
        include_usage=include_usage,
        max_tokens=limit,
        payload_bytes=size,
    )


def ids_for(reply: ModelReply, make: Any, api: Api) -> Ids:
    prefixes = {
        "chat": ("chatcmpl-", "msg_", "call_"),
        "responses": ("resp_", "msg_", "call_"),
        "messages": ("msg_", "msg_", "toolu_"),
    }[api]
    return Ids(
        make(prefixes[0]), make(prefixes[1]), tuple(make(prefixes[2]) for _ in reply.tool_calls)
    )


def _sse(data: dict[str, Any], event: str | None = None) -> bytes:
    head = f"event: {event}\n" if event else ""
    return f"{head}data: {json.dumps(data, separators=(',', ':'))}\n\n".encode()


# Chat Completions -------------------------------------------------------------------------


def _chat_usage(reply: ModelReply) -> dict[str, int]:
    return {
        "prompt_tokens": reply.input_tokens,
        "completion_tokens": reply.output_tokens,
        "total_tokens": reply.input_tokens + reply.output_tokens,
    }


def _chat_finish(reply: ModelReply) -> str:
    return "tool_calls" if reply.tool_calls else "length" if reply.truncated else "stop"


def chat_body(request: ModelRequest, reply: ModelReply, ids: Ids) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": reply.text, "refusal": None}
    if reply.tool_calls:
        message["tool_calls"] = [
            {
                "id": ids.calls[i],
                "type": "function",
                "function": {"name": c.name, "arguments": c.arguments},
            }
            for i, c in enumerate(reply.tool_calls)
        ]
    return {
        "id": ids.response,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": request.model,
        "choices": [
            {"index": 0, "message": message, "logprobs": None, "finish_reason": _chat_finish(reply)}
        ],
        "usage": _chat_usage(reply),
    }


def chat_stream(request: ModelRequest, reply: ModelReply, ids: Ids) -> Iterator[bytes]:
    base = {
        "id": ids.response,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": request.model,
    }

    def chunk(delta: dict[str, Any], finish: str | None = None) -> bytes:
        return _sse(
            {
                **base,
                "choices": [
                    {"index": 0, "delta": delta, "logprobs": None, "finish_reason": finish}
                ],
            }
        )

    yield chunk({"role": "assistant", "content": ""})
    for piece in _PIECES.findall(reply.text or ""):
        yield chunk({"content": piece})
    for index, call in enumerate(reply.tool_calls):
        yield chunk(
            {
                "tool_calls": [
                    {
                        "index": index,
                        "id": ids.calls[index],
                        "type": "function",
                        "function": {"name": call.name, "arguments": ""},
                    }
                ]
            }
        )
        yield chunk({"tool_calls": [{"index": index, "function": {"arguments": call.arguments}}]})
    yield chunk({}, _chat_finish(reply))
    if request.include_usage:
        yield _sse({**base, "choices": [], "usage": _chat_usage(reply)})
    yield b"data: [DONE]\n\n"


# Responses --------------------------------------------------------------------------------


def _response_items(reply: ModelReply, ids: Ids, *, done: bool = True) -> list[dict[str, Any]]:
    status = "completed" if done else "in_progress"
    if reply.tool_calls:
        return [
            {
                "type": "function_call",
                "id": f"fc_{ids.calls[i][5:]}",
                "call_id": ids.calls[i],
                "name": c.name,
                "arguments": c.arguments if done else "",
                "status": status,
            }
            for i, c in enumerate(reply.tool_calls)
        ]
    content = (
        [{"type": "output_text", "text": reply.text or "", "annotations": [], "logprobs": []}]
        if done
        else []
    )
    return [
        {
            "type": "message",
            "id": ids.message,
            "status": status,
            "role": "assistant",
            "content": content,
        }
    ]


def responses_body(
    request: ModelRequest, reply: ModelReply, ids: Ids, *, done: bool = True
) -> dict[str, Any]:
    return {
        "id": ids.response,
        "object": "response",
        "created_at": int(time.time()),
        "status": ("incomplete" if reply.truncated else "completed") if done else "in_progress",
        "model": request.model,
        "output": _response_items(reply, ids) if done else [],
        "usage": {
            "input_tokens": reply.input_tokens,
            "output_tokens": reply.output_tokens,
            "total_tokens": reply.input_tokens + reply.output_tokens,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens_details": {"reasoning_tokens": 0},
        }
        if done
        else None,
        "error": None,
        "incomplete_details": {"reason": "max_output_tokens"} if done and reply.truncated else None,
        "instructions": None,
        "metadata": {},
        "parallel_tool_calls": True,
        "temperature": None,
        "tool_choice": "auto",
        "tools": [],
        "top_p": None,
    }


def responses_stream(request: ModelRequest, reply: ModelReply, ids: Ids) -> Iterator[bytes]:
    sequence = iter(range(1_000_000))

    def event(kind: str, **fields: Any) -> bytes:
        return _sse({"type": kind, "sequence_number": next(sequence), **fields}, kind)

    yield event("response.created", response=responses_body(request, reply, ids, done=False))
    yield event("response.in_progress", response=responses_body(request, reply, ids, done=False))
    pending = _response_items(reply, ids, done=False)
    finished = _response_items(reply, ids)
    for index, (start, item) in enumerate(zip(pending, finished, strict=True)):
        yield event("response.output_item.added", output_index=index, item=start)
        if item["type"] == "message":
            part = {"type": "output_text", "text": "", "annotations": [], "logprobs": []}
            yield event(
                "response.content_part.added",
                item_id=item["id"],
                output_index=index,
                content_index=0,
                part=part,
            )
            for piece in _PIECES.findall(reply.text or ""):
                yield event(
                    "response.output_text.delta",
                    item_id=item["id"],
                    output_index=index,
                    content_index=0,
                    delta=piece,
                    logprobs=[],
                )
            yield event(
                "response.output_text.done",
                item_id=item["id"],
                output_index=index,
                content_index=0,
                text=reply.text or "",
                logprobs=[],
            )
            yield event(
                "response.content_part.done",
                item_id=item["id"],
                output_index=index,
                content_index=0,
                part=item["content"][0],
            )
        else:
            yield event(
                "response.function_call_arguments.delta",
                item_id=item["id"],
                output_index=index,
                delta=item["arguments"],
            )
            yield event(
                "response.function_call_arguments.done",
                item_id=item["id"],
                output_index=index,
                name=item["name"],
                arguments=item["arguments"],
            )
        yield event("response.output_item.done", output_index=index, item=item)
    kind = "response.incomplete" if reply.truncated else "response.completed"
    yield event(kind, response=responses_body(request, reply, ids))


# Anthropic Messages -------------------------------------------------------------------------


def _anthropic_stop(reply: ModelReply) -> str:
    return "tool_use" if reply.tool_calls else "max_tokens" if reply.truncated else "end_turn"


def _anthropic_usage(reply: ModelReply, *, output: int | None = None) -> dict[str, int]:
    return {
        "input_tokens": reply.input_tokens,
        "output_tokens": reply.output_tokens if output is None else output,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
    }


def _anthropic_blocks(reply: ModelReply, ids: Ids) -> list[dict[str, Any]]:
    if reply.tool_calls:
        return [
            {
                "type": "tool_use",
                "id": ids.calls[i],
                "name": c.name,
                "input": json.loads(c.arguments),
            }
            for i, c in enumerate(reply.tool_calls)
        ]
    return [{"type": "text", "text": reply.text or ""}]


def messages_body(request: ModelRequest, reply: ModelReply, ids: Ids) -> dict[str, Any]:
    return {
        "id": ids.response,
        "type": "message",
        "role": "assistant",
        "model": request.model,
        "content": _anthropic_blocks(reply, ids),
        "stop_reason": _anthropic_stop(reply),
        "stop_sequence": None,
        "usage": _anthropic_usage(reply),
    }


def messages_stream(request: ModelRequest, reply: ModelReply, ids: Ids) -> Iterator[bytes]:
    start = {
        **messages_body(request, reply, ids),
        "content": [],
        "stop_reason": None,
        "usage": _anthropic_usage(reply, output=1),
    }
    yield _sse({"type": "message_start", "message": start}, "message_start")
    for index, block in enumerate(_anthropic_blocks(reply, ids)):
        if block["type"] == "text":
            yield _sse(
                {
                    "type": "content_block_start",
                    "index": index,
                    "content_block": {"type": "text", "text": ""},
                },
                "content_block_start",
            )
            for piece in _PIECES.findall(block["text"]):
                yield _sse(
                    {
                        "type": "content_block_delta",
                        "index": index,
                        "delta": {"type": "text_delta", "text": piece},
                    },
                    "content_block_delta",
                )
        else:
            yield _sse(
                {
                    "type": "content_block_start",
                    "index": index,
                    "content_block": {**block, "input": {}},
                },
                "content_block_start",
            )
            yield _sse(
                {
                    "type": "content_block_delta",
                    "index": index,
                    "delta": {
                        "type": "input_json_delta",
                        "partial_json": json.dumps(block["input"]),
                    },
                },
                "content_block_delta",
            )
        yield _sse({"type": "content_block_stop", "index": index}, "content_block_stop")
    yield _sse(
        {
            "type": "message_delta",
            "delta": {"stop_reason": _anthropic_stop(reply), "stop_sequence": None},
            "usage": {"output_tokens": reply.output_tokens},
        },
        "message_delta",
    )
    yield _sse({"type": "message_stop"}, "message_stop")


RENDER = {"chat": chat_body, "responses": responses_body, "messages": messages_body}
STREAM = {"chat": chat_stream, "responses": responses_stream, "messages": messages_stream}
