import asyncio
import json
from typing import Annotated, Literal

import httpx
from pydantic import BaseModel, Field, SecretStr, StringConstraints

from runweave.agents.context import FINISH as FINISH
from runweave.agents.context import model_payload
from runweave.agents.contracts import (
    IncidentRequest,
    IncidentResult,
    ModelDecision,
    ToolCall,
    ToolExchange,
)
from runweave.agents.provider_wire import (
    ENDPOINTS,
    AnthropicCall,
    AnthropicEnvelope,
    LiveProvider,
    request_data,
)
from runweave.budgets import current_reservation
from runweave.deadline import check_budget
from runweave.memory import refresh_recall

GROQ_URL = ENDPOINTS["groq"]
_MAX_RESPONSE_BYTES = 64 * 1024


class GroqFunction(BaseModel):
    name: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    arguments: Annotated[str, StringConstraints(min_length=1, max_length=4096)]


class GroqCall(BaseModel):
    id: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    type: Literal["function"]
    function: GroqFunction


class GroqMessage(BaseModel):
    tool_calls: Annotated[list[GroqCall], Field(min_length=1, max_length=1)]


class GroqChoice(BaseModel):
    finish_reason: Literal["tool_calls"]
    message: GroqMessage


class GroqUsage(BaseModel):
    completion_tokens: Annotated[int, Field(gt=0)] | None = None
    prompt_tokens: Annotated[int, Field(ge=0)] | None = None


class GroqUsageEnvelope(BaseModel):
    usage: GroqUsage | None = None


class GroqResponse(BaseModel):
    choices: Annotated[list[GroqChoice], Field(min_length=1, max_length=1)]


class ProviderAgentModel:
    def __init__(
        self,
        client: httpx.AsyncClient,
        api_key: SecretStr,
        *,
        model: str,
        concurrency: int,
        requests_per_minute: int,
        provider: LiveProvider = "groq",
    ) -> None:
        self._client = client
        self._api_key = api_key
        self._model = model
        self._provider = provider
        self._slots = asyncio.Semaphore(concurrency)
        self._rate_lock = asyncio.Lock()
        self._interval = 60 / requests_per_minute
        self._next_request = 0.0

    async def decide(
        self,
        incident: IncidentRequest,
        history: tuple[ToolExchange, ...],
        tools: tuple[dict[str, object], ...],
        completion_budget: int,
    ) -> ModelDecision:
        output_limit = min(512, completion_budget)
        async with self._slots:
            async with self._rate_lock:
                loop = asyncio.get_running_loop()
                delay = self._next_request - loop.time()
                if delay > 0:
                    await asyncio.sleep(delay)
                check_budget()
                self._next_request = loop.time() + self._interval

            await refresh_recall()
            payload = model_payload(incident, history, tools, self._model, output_limit)
            payload, headers = request_data(
                self._provider, payload, self._api_key.get_secret_value().strip()
            )
            body = bytearray()
            async with self._client.stream(
                "POST",
                ENDPOINTS[self._provider],
                json=payload,
                headers=headers,
                follow_redirects=False,
            ) as response:
                response.raise_for_status()
                async for chunk in response.aiter_bytes(chunk_size=8192):
                    if len(body) + len(chunk) > _MAX_RESPONSE_BYTES:
                        raise ValueError("Model response exceeds the size limit")
                    body.extend(chunk)

        input_tokens: int | None
        completion_tokens: int | None
        if self._provider == "anthropic":
            envelope = AnthropicEnvelope.model_validate_json(body, strict=True)
            claude = envelope.usage
            input_tokens = (
                claude.input_tokens
                + claude.cache_creation_input_tokens
                + claude.cache_read_input_tokens
            )
            completion_tokens = claude.output_tokens
        else:
            usage = GroqUsageEnvelope.model_validate_json(body, strict=True).usage
            input_tokens = usage.prompt_tokens if usage else None
            completion_tokens = usage.completion_tokens if usage else None
        reservation = current_reservation()
        if reservation is not None:
            await reservation.settle(input_tokens, completion_tokens)
        if completion_tokens is not None and not 0 < completion_tokens <= output_limit:
            raise ValueError("Invalid model completion token usage")
        if self._provider == "anthropic":
            calls = [block for block in envelope.content if block.get("type") == "tool_use"]
            if envelope.stop_reason != "tool_use" or len(calls) != 1:
                raise ValueError("Expected one Claude tool call")
            call = AnthropicCall.model_validate(calls[0], strict=True)
            call_id, name, arguments = call.id, call.name, json.dumps(call.input)
        else:
            parsed = GroqResponse.model_validate_json(body, strict=True)
            proposed = parsed.choices[0].message.tool_calls[0]
            call_id, name, arguments = (
                proposed.id,
                proposed.function.name,
                proposed.function.arguments,
            )
        action: ToolCall | IncidentResult
        if name == FINISH:
            action = IncidentResult.model_validate_json(arguments, strict=True)
        else:
            action = ToolCall(id=call_id, name=name, arguments=arguments)
        return ModelDecision(action, completion_tokens or output_limit, input_tokens)
