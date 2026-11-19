"""Passthrough to the developer's provider. RunWeave holds the key; agents never see it.

The request body is forwarded unchanged except that its output-token limit is capped at
the call's budget reservation and streamed Chat Completions always ask for usage (the
added usage chunk is removed again if the client did not ask for it). Redirects are never
followed and responses are size-limited.
"""

import asyncio
import json
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import httpx

from runweave.limits import ProviderLimiter
from runweave.proxy.mock import Api

ANTHROPIC_VERSION = "2023-06-01"
_RELAYED_HEADERS = ("content-type", "retry-after")


class UpstreamFailure(Exception):
    def __init__(self, code: str, *, charged: bool) -> None:
        self.code = code
        self.charged = charged
        super().__init__(code)


@dataclass(frozen=True)
class Provider:
    base_url: str
    key: Callable[[], str]
    limiter: ProviderLimiter


@dataclass
class Usage:
    input_tokens: int | None = None
    output_tokens: int | None = None

    def add_anthropic(self, usage: Mapping[str, Any], *, start: bool) -> None:
        if start:
            self.input_tokens = sum(
                int(usage.get(k) or 0)
                for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
            )
        if isinstance(usage.get("output_tokens"), int) and not start:
            self.output_tokens = usage["output_tokens"]


def usage_from_body(api: Api, body: Any) -> Usage:
    usage = body.get("usage") if isinstance(body, dict) else None
    if not isinstance(usage, dict):
        return Usage()
    if api == "chat":
        return Usage(_int(usage.get("prompt_tokens")), _int(usage.get("completion_tokens")))
    if api == "messages":
        found = Usage()
        found.add_anthropic(usage, start=True)
        found.output_tokens = _int(usage.get("output_tokens"))
        return found
    return Usage(_int(usage.get("input_tokens")), _int(usage.get("output_tokens")))


def _int(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None


@dataclass(frozen=True)
class Prepared:
    url: str
    headers: dict[str, str]
    body: dict[str, Any]
    strip_usage: bool


@dataclass
class Relayed:
    status: int
    headers: dict[str, str]
    content: bytes


class Upstream:
    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        openai: Provider,
        anthropic: Provider,
        max_response_bytes: int,
        connect_timeout: float = 10,
    ) -> None:
        self._client = client
        self._providers = {"openai": openai, "anthropic": anthropic}
        self._max_bytes = max_response_bytes
        self._connect_timeout = connect_timeout

    def configured(self, api: Api) -> bool:
        return bool(self._provider(api).key())

    def _provider(self, api: Api) -> Provider:
        return self._providers["anthropic" if api == "messages" else "openai"]

    def limiter(self, api: Api) -> ProviderLimiter:
        return self._providers["anthropic" if api == "messages" else "openai"].limiter

    def prepare(
        self, api: Api, body: dict[str, Any], output_limit: int, client_headers: Mapping[str, str]
    ) -> Prepared:
        body = dict(body)
        strip_usage = False
        if api == "chat":
            field = "max_tokens" if "max_tokens" in body else "max_completion_tokens"
            body[field] = min(int(body.get(field) or output_limit), output_limit)
            if body.get("stream") is True:
                options = dict(body.get("stream_options") or {})
                strip_usage = options.get("include_usage") is not True
                body["stream_options"] = {**options, "include_usage": True}
        elif api == "responses":
            body["max_output_tokens"] = min(
                int(body.get("max_output_tokens") or output_limit), output_limit
            )
        else:
            body["max_tokens"] = min(int(body.get("max_tokens") or output_limit), output_limit)
        if api == "messages":
            provider = self._providers["anthropic"]
            headers = {
                "x-api-key": provider.key(),
                "anthropic-version": client_headers.get("anthropic-version", ANTHROPIC_VERSION),
            }
            if "anthropic-beta" in client_headers:
                headers["anthropic-beta"] = client_headers["anthropic-beta"]
            url = f"{provider.base_url}/v1/messages"
        else:
            provider = self._providers["openai"]
            headers = {"Authorization": f"Bearer {provider.key()}"}
            url = f"{provider.base_url}/{'chat/completions' if api == 'chat' else 'responses'}"
        headers.update({"Content-Type": "application/json", "Accept": "application/json"})
        return Prepared(url, headers, body, strip_usage)

    def _timeout(self, expires_at: float) -> httpx.Timeout:
        remaining = max(0.1, expires_at - asyncio.get_running_loop().time())
        connect = min(self._connect_timeout, remaining)
        return httpx.Timeout(remaining, connect=connect, pool=connect)

    @asynccontextmanager
    async def open(self, prepared: Prepared, expires_at: float) -> AsyncIterator[httpx.Response]:
        try:
            async with self._client.stream(
                "POST",
                prepared.url,
                json=prepared.body,
                headers=prepared.headers,
                timeout=self._timeout(expires_at),
                follow_redirects=False,
            ) as response:
                if 300 <= response.status_code < 400:
                    raise UpstreamFailure("UPSTREAM_REDIRECT_DENIED", charged=False)
                yield response
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
            raise UpstreamFailure("UPSTREAM_UNAVAILABLE", charged=False) from None
        except httpx.HTTPError:
            # The provider may have started generating; usage is unknown.
            raise UpstreamFailure("UPSTREAM_CONNECTION_LOST", charged=True) from None

    async def read(self, response: httpx.Response) -> Relayed:
        data = bytearray()
        async for chunk in response.aiter_bytes():
            data.extend(chunk)
            if len(data) > self._max_bytes:
                raise UpstreamFailure("UPSTREAM_RESPONSE_TOO_LARGE", charged=True)
        headers = {k: response.headers[k] for k in _RELAYED_HEADERS if k in response.headers}
        return Relayed(response.status_code, headers, bytes(data))

    async def events(
        self, api: Api, response: httpx.Response, usage: Usage, *, strip_usage: bool
    ) -> AsyncIterator[bytes]:
        """Relay server-sent events, reading usage from them as they pass."""
        size = 0
        lines: list[str] = []
        async for line in response.aiter_lines():
            size += len(line) + 1
            if size > self._max_bytes:
                raise UpstreamFailure("UPSTREAM_RESPONSE_TOO_LARGE", charged=True)
            if line:
                lines.append(line)
                continue
            if lines:
                event = self._observe(api, lines, usage, strip_usage)
                lines = []
                if event is not None:
                    yield event
        if lines:
            event = self._observe(api, lines, usage, strip_usage)
            if event is not None:
                yield event

    @staticmethod
    def _observe(api: Api, lines: list[str], usage: Usage, strip_usage: bool) -> bytes | None:
        data_lines = [line[5:].lstrip() for line in lines if line.startswith("data:")]
        payload: Any = None
        if data_lines and data_lines[0] != "[DONE]":
            try:
                payload = json.loads("\n".join(data_lines))
            except ValueError:
                payload = None
        if isinstance(payload, dict):
            kind = payload.get("type")
            if api == "chat" and isinstance(payload.get("usage"), dict):
                found = usage_from_body("chat", payload)
                usage.input_tokens, usage.output_tokens = found.input_tokens, found.output_tokens
            elif api == "responses" and kind in {"response.completed", "response.incomplete"}:
                found = usage_from_body("responses", payload.get("response"))
                usage.input_tokens, usage.output_tokens = found.input_tokens, found.output_tokens
            elif api == "messages" and kind == "message_start":
                message = payload.get("message") or {}
                usage.add_anthropic(message.get("usage") or {}, start=True)
            elif api == "messages" and kind == "message_delta":
                usage.add_anthropic(payload.get("usage") or {}, start=False)
            if api == "chat" and strip_usage:
                if payload.get("choices") == [] and "usage" in payload:
                    return None
                if "usage" in payload:
                    payload = {k: v for k, v in payload.items() if k != "usage"}
                    return f"data: {json.dumps(payload, separators=(',', ':'))}\n\n".encode()
        return ("\n".join(lines) + "\n\n").encode()
