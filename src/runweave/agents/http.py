"""RunWeave's side of the HTTP agent contract (`runweave.agent/v1`).

Outbound rules: loopback destinations unless a host is explicitly allowed at startup,
no redirects, bounded responses, and retries only for failures where the agent cannot
have started the run (connection failures) or asked to be retried (429/502/503/504).
Every retry sends the same Idempotency-Key, the run ID.
"""

import asyncio
import ipaddress
import json
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from random import uniform
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import httpx
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from pydantic import BaseModel, RootModel, ValidationError

from runweave.agents.catalog import TRIAGE_AGENT, AgentInfo, SchemaViolation
from runweave.deadline import RUN_DEADLINE, DeadlineExceeded, check_budget, remaining_budget
from runweave.http_contract import (
    CONTRACT_VERSION,
    MANIFEST_PATH,
    MAX_DEADLINE_MS,
    REPORTED_FAILURES,
    AgentDescriptor,
    ErrorResponse,
    Manifest,
    ProxyBinding,
    RunRequest,
    RunResponse,
    cancel_path,
    run_path,
)
from runweave.sdk import (
    AgentError,
    AgentOutputInvalid,
    AgentUnavailable,
    RunContext,
    StepLimitExceeded,
)
from runweave.tools.retry import retry_after_seconds

__all__ = [
    "AgentUrlNotAllowed",
    "HttpAgent",
    "HttpAgentConnector",
    "ManifestRejected",
    "RetryPolicy",
    "SchemaViolation",
    "normalize_agent_url",
]

TRANSIENT_STATUS = frozenset({429, 502, 503, 504})
CANCEL_TIMEOUT_SECONDS = 1.0
MAX_URL_LENGTH = 2048


class AgentUrlNotAllowed(ValueError):
    code = "AGENT_URL_NOT_ALLOWED"


class ManifestRejected(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class AgentConnectionLost(AgentError):
    code = "AGENT_CONNECTION_LOST"


class AgentRedirectDenied(AgentError):
    code = "AGENT_REDIRECT_DENIED"


class AgentResponseInvalid(AgentError):
    code = "AGENT_RESPONSE_INVALID"


class AgentResponseTooLarge(AgentError):
    code = "AGENT_RESPONSE_TOO_LARGE"


class AgentServerError(AgentError):
    code = "AGENT_SERVER_ERROR"


class AgentRequestRejected(AgentError):
    code = "AGENT_REQUEST_REJECTED"


class AgentInputRejected(AgentError):
    code = "AGENT_INPUT_REJECTED"


class AgentReportedFailure(AgentError):
    """A failure code from the agent-side SDK that RunWeave lets through unchanged."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__()


def normalize_agent_url(url: str, *, allowed_hosts: Sequence[str]) -> str:
    """Return the canonical base URL or raise AgentUrlNotAllowed."""
    if len(url) > MAX_URL_LENGTH or not url.isascii() or any(c.isspace() for c in url):
        raise AgentUrlNotAllowed("Use an http(s) URL without spaces")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise AgentUrlNotAllowed("The URL is not valid") from None
    host = (parts.hostname or "").lower()
    if parts.scheme not in {"http", "https"} or not host:
        raise AgentUrlNotAllowed("Use an http:// or https:// URL")
    if parts.username is not None or parts.password is not None:
        raise AgentUrlNotAllowed("Credentials are not allowed in agent URLs")
    if parts.query or parts.fragment or "?" in url or "#" in url:
        raise AgentUrlNotAllowed("Agent URLs cannot have a query or fragment")
    if not (_is_loopback(host) or host in {h.lower() for h in allowed_hosts}):
        raise AgentUrlNotAllowed(
            "HTTP agents must be on loopback (127.0.0.1, ::1 or localhost) "
            "unless the host is in RUNWEAVE_HTTP_AGENT_ALLOWED_HOSTS"
        )
    netloc = f"[{host}]" if ":" in host else host
    if port is not None:
        netloc = f"{netloc}:{port}"
    return urlunsplit((parts.scheme, netloc, parts.path.rstrip("/"), "", ""))


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@dataclass(frozen=True)
class RetryPolicy:
    attempts: int
    base_delay: float
    max_delay: float

    def delay(self, attempt: int, retry_after: float) -> float:
        cap = min(self.max_delay, self.base_delay * 2**attempt)
        return max(retry_after, uniform(cap / 2, cap))


class JsonPayload(RootModel[dict[str, Any]]):
    """Agent input or output validated against a JSON Schema rather than a Pydantic model."""


_SCHEMA_MESSAGES = {
    "required": "Field required",
    "type": "Value has the wrong type",
    "enum": "Value is not one of the allowed options",
    "const": "Value is not the allowed value",
    "minLength": "Value is too short",
    "maxLength": "Value is too long",
    "pattern": "Value does not match the required pattern",
    "minimum": "Value is too small",
    "exclusiveMinimum": "Value is too small",
    "maximum": "Value is too large",
    "exclusiveMaximum": "Value is too large",
    "minItems": "Too few items",
    "maxItems": "Too many items",
    "additionalProperties": "Unexpected field",
}


def _schema_errors(validator: Draft202012Validator, value: Any) -> list[dict[str, Any]]:
    # Messages come from a fixed table keyed by the failed rule. jsonschema's own messages
    # quote the submitted value, so they are never returned.
    errors: list[dict[str, Any]] = []
    for error in sorted(validator.iter_errors(value), key=lambda e: list(map(str, e.path))):
        location = list(error.absolute_path)
        rule = str(error.validator)
        if rule == "required" and isinstance(error.instance, dict):
            missing = [k for k in error.validator_value if k not in error.instance]
            location.extend(missing[:1])
        errors.append(
            {
                "loc": location,
                "type": f"schema_{rule}"[:64],
                "msg": _SCHEMA_MESSAGES.get(rule, "Value does not match the schema"),
            }
        )
        if len(errors) == 20:
            break
    return errors


def _compile(schema: dict[str, Any]) -> Draft202012Validator:
    Draft202012Validator.check_schema(schema)
    _require_local_references(schema)
    return Draft202012Validator(schema)


def _require_local_references(node: Any, depth: int = 0) -> None:
    # Remote references would make validation fetch or fail at run time; only "#..." is
    # allowed, which covers the $defs that Pydantic and most generators emit.
    if depth > 64:
        raise SchemaError("Schema nesting is too deep")
    if isinstance(node, dict):
        for key in ("$ref", "$dynamicRef"):
            if key in node and not (isinstance(node[key], str) and node[key].startswith("#")):
                raise SchemaError("Only local references (#...) are supported")
        for value in node.values():
            _require_local_references(value, depth + 1)
    elif isinstance(node, list):
        for value in node:
            _require_local_references(value, depth + 1)


class HttpAgent:
    def __init__(
        self,
        connector: "HttpAgentConnector",
        base_url: str,
        descriptor: AgentDescriptor,
        *,
        removable: bool,
    ) -> None:
        self._connector = connector
        self._base_url = base_url
        self._input = _compile(descriptor.input_schema)
        self._output = _compile(descriptor.output_schema)
        self.info = AgentInfo(
            name=descriptor.name,
            version=descriptor.version,
            description=descriptor.description,
            kind="http",
            input_schema=descriptor.input_schema,
            output_schema=descriptor.output_schema,
            max_steps=descriptor.max_steps,
            target=base_url,
            removable=removable,
        )

    def parse(self, payload: Mapping[str, Any]) -> BaseModel:
        value = dict(payload) if isinstance(payload, Mapping) else payload
        try:
            errors = _schema_errors(self._input, value)
        except Exception:  # noqa: BLE001 - an unevaluable schema rejects input, never crashes
            errors = [{"loc": [], "type": "schema_error", "msg": "Input could not be validated"}]
        if errors:
            raise SchemaViolation(errors)
        return JsonPayload(value)

    async def run(self, input: BaseModel, context: RunContext) -> BaseModel:
        run_id = context.run_id or uuid4()
        remaining = remaining_budget()
        deadline_ms = MAX_DEADLINE_MS if remaining == float("inf") else int(remaining * 1000)
        request = RunRequest(
            run_id=run_id,
            attempt=context.attempt,
            input=input.model_dump(mode="json"),
            deadline_ms=max(1, min(MAX_DEADLINE_MS, deadline_ms)),
            proxy=None
            if context.proxy is None
            else ProxyBinding(
                token=context.proxy.token,
                openai_base_url=context.proxy.openai_base_url,
                anthropic_base_url=context.proxy.anthropic_base_url,
            ),
        )
        url = self._base_url + run_path(self.info.name)
        try:
            status, headers, body = await self._connector.send_run(url, request)
        except asyncio.CancelledError:
            await self._connector.cancel(self._base_url + cancel_path(self.info.name, run_id))
            raise
        response = self._run_response(status, headers, body)
        try:
            valid = self._output.is_valid(response.output)
        except Exception:  # noqa: BLE001 - an unevaluable schema rejects the output
            valid = False
        if not valid:
            raise AgentOutputInvalid()
        if len(response.steps) > self.info.max_steps:
            raise StepLimitExceeded()
        await context.add_finished_steps(
            [(s.title, s.detail, s.status, s.duration_ms) for s in response.steps]
        )
        return JsonPayload(response.output)

    @staticmethod
    def _run_response(status: int, headers: httpx.Headers, body: bytes) -> RunResponse:
        if status == 200:
            if not _is_json(headers):
                raise AgentResponseInvalid()
            try:
                return RunResponse.model_validate_json(body)
            except ValidationError:
                raise AgentResponseInvalid() from None
        if status == 422:
            raise AgentInputRejected()
        if 500 <= status < 600:
            code = _reported_code(headers, body)
            if code == "AGENT_DEADLINE_EXCEEDED":
                raise DeadlineExceeded(RUN_DEADLINE)
            if code == StepLimitExceeded.code:
                raise StepLimitExceeded()
            if code == AgentOutputInvalid.code:
                raise AgentOutputInvalid()
            if code is not None:
                raise AgentReportedFailure(code)
            raise AgentServerError()
        if 400 <= status < 500:
            raise AgentRequestRejected()
        raise AgentResponseInvalid()


def _is_json(headers: httpx.Headers) -> bool:
    content_type: str = headers.get("content-type", "")
    return content_type.split(";")[0].strip().lower() == "application/json"


def _reported_code(headers: httpx.Headers, body: bytes) -> str | None:
    if not _is_json(headers):
        return None
    try:
        code = ErrorResponse.model_validate_json(body).error.code
    except ValidationError:
        return None
    return code if code in REPORTED_FAILURES else None


class _Transient(Exception):
    def __init__(self, retry_after: float = 0.0) -> None:
        self.retry_after = retry_after


class HttpAgentConnector:
    """Discovers and calls HTTP agents through the process's shared HTTP client."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        allowed_hosts: Sequence[str],
        retry: RetryPolicy,
        max_response_bytes: int,
        connect_timeout: float,
    ) -> None:
        self._client = client
        self._allowed_hosts = tuple(allowed_hosts)
        self._retry = retry
        self._max_bytes = max_response_bytes
        self._connect_timeout = connect_timeout

    def normalize(self, url: str) -> str:
        return normalize_agent_url(url, allowed_hosts=self._allowed_hosts)

    async def discover(self, url: str) -> tuple[str, Manifest]:
        base = self.normalize(url)
        try:
            status, headers, body = await self._receive(
                "GET",
                base + MANIFEST_PATH,
                limits=httpx.Timeout(self._connect_timeout * 5, connect=self._connect_timeout),
            )
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
            raise ManifestRejected(
                "AGENT_UNREACHABLE", f"No agent is listening at {base}"
            ) from None
        except httpx.HTTPError:
            raise ManifestRejected("AGENT_UNREACHABLE", f"{base} did not answer") from None
        except AgentRedirectDenied:
            raise ManifestRejected(
                "AGENT_REDIRECT_DENIED", "The agent answered with a redirect"
            ) from None
        except AgentResponseTooLarge:
            raise ManifestRejected(
                "AGENT_MANIFEST_INVALID", "The agent manifest is too large"
            ) from None
        if status != 200 or not _is_json(headers):
            raise ManifestRejected(
                "AGENT_MANIFEST_INVALID",
                f"{MANIFEST_PATH} answered HTTP {status}; is this a RunWeave agent?",
            )
        try:
            raw = json.loads(body)
        except ValueError:
            raise ManifestRejected("AGENT_MANIFEST_INVALID", "The manifest is not JSON") from None
        if isinstance(raw, dict) and raw.get("contract") != CONTRACT_VERSION:
            raise ManifestRejected(
                "AGENT_MANIFEST_INVALID", f"The agent does not speak {CONTRACT_VERSION}"
            )
        try:
            manifest = Manifest.model_validate(raw)
        except ValidationError:
            raise ManifestRejected("AGENT_MANIFEST_INVALID", "The manifest is malformed") from None
        names = [entry.name for entry in manifest.agents]
        if len(set(names)) != len(names) or TRIAGE_AGENT in names:
            raise ManifestRejected(
                "AGENT_MANIFEST_INVALID", "Manifest agent names must be unique and not reserved"
            )
        for entry in manifest.agents:
            if entry.input_schema.get("type") != "object":
                raise ManifestRejected(
                    "AGENT_MANIFEST_INVALID", f"{entry.name}: input_schema must be an object"
                )
            try:
                _compile(entry.input_schema)
                _compile(entry.output_schema)
            except SchemaError:
                raise ManifestRejected(
                    "AGENT_MANIFEST_INVALID", f"{entry.name}: invalid JSON Schema"
                ) from None
        return base, manifest

    async def connect(self, url: str, *, removable: bool) -> list[HttpAgent]:
        base, manifest = await self.discover(url)
        return [HttpAgent(self, base, entry, removable=removable) for entry in manifest.agents]

    async def send_run(self, url: str, request: RunRequest) -> tuple[int, httpx.Headers, bytes]:
        body = request.model_dump(mode="json")
        headers = {"Idempotency-Key": str(request.run_id), "RunWeave-Contract": CONTRACT_VERSION}
        for attempt in range(self._retry.attempts):
            check_budget()
            remaining = remaining_budget()
            # The run deadline cancels the request; the read timeout only backs it up.
            read = None if remaining == float("inf") else remaining + 1
            try:
                status, response_headers, data = await self._receive(
                    "POST",
                    url,
                    json=body,
                    headers=headers,
                    limits=httpx.Timeout(
                        read, connect=self._connect_timeout, pool=self._connect_timeout
                    ),
                )
                if status in TRANSIENT_STATUS:
                    raise _Transient(
                        retry_after_seconds(response_headers.get("retry-after"), datetime.now(UTC))
                    )
                return status, response_headers, data
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
                retry_after = 0.0
            except _Transient as transient:
                retry_after = transient.retry_after
            except httpx.HTTPError:
                # The agent may have started the run; only RunWeave's next attempt (with the
                # same idempotency key) after a lease loss could safely repeat it.
                raise AgentConnectionLost() from None
            if attempt + 1 == self._retry.attempts:
                break
            delay = self._retry.delay(attempt, retry_after)
            if delay > self._retry.max_delay or delay >= remaining_budget():
                break
            await asyncio.sleep(delay)
        raise AgentUnavailable()

    async def _receive(
        self, method: str, url: str, *, limits: httpx.Timeout, **kwargs: Any
    ) -> tuple[int, httpx.Headers, bytes]:
        async with self._client.stream(
            method,
            url,
            timeout=limits,
            follow_redirects=False,
            headers={"Accept": "application/json", **kwargs.pop("headers", {})},
            **kwargs,
        ) as response:
            if response.is_redirect or 300 <= response.status_code < 400:
                raise AgentRedirectDenied()
            declared = response.headers.get("content-length")
            if declared is not None and declared.isdecimal() and int(declared) > self._max_bytes:
                raise AgentResponseTooLarge()
            data = bytearray()
            async for chunk in response.aiter_bytes():
                data.extend(chunk)
                if len(data) > self._max_bytes:
                    raise AgentResponseTooLarge()
            return response.status_code, response.headers, bytes(data)

    async def cancel(self, url: str) -> None:
        """Best effort: tell the agent to stop. Failures never mask the cancellation."""
        with suppress(Exception):
            async with asyncio.timeout(CANCEL_TIMEOUT_SECONDS):
                await self._client.post(
                    url,
                    timeout=CANCEL_TIMEOUT_SECONDS,
                    follow_redirects=False,
                    headers={"RunWeave-Contract": CONTRACT_VERSION},
                )
