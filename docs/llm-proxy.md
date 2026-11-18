# LLM proxy

RunWeave serves OpenAI- and Anthropic-compatible endpoints so your agent's model calls are
metered, budgeted and shown on the run timeline, without changing agent code beyond the
base URL and API key. In this release the proxy runs in **mock mode**: deterministic,
offline and free. Passthrough to a real provider and fault injection come next.

| API | Base URL to give the SDK | Endpoint |
| --- | --- | --- |
| OpenAI Chat Completions | `http://127.0.0.1:8000/llm/v1` | `POST /llm/v1/chat/completions` |
| OpenAI Responses | `http://127.0.0.1:8000/llm/v1` | `POST /llm/v1/responses` |
| Anthropic Messages | `http://127.0.0.1:8000/llm` | `POST /llm/v1/messages` |

All three support streaming (server-sent events in each provider's own format), tool calls
and usage reporting, including `stream_options.include_usage` for Chat Completions.

## Use it from an agent

Every run attempt gets its own proxy token. Use it as the API key:

```python
from openai import AsyncOpenAI
from runweave.sdk import RunContext, agent


@agent(name="my-agent", version="1.0.0", input=Question, output=Answer)
async def run(input: Question, ctx: RunContext) -> Answer:
    client = AsyncOpenAI(api_key=ctx.proxy.token, base_url=ctx.proxy.openai_base_url)
    reply = await client.chat.completions.create(model="any", messages=[...])
    ...
```

- `ctx.proxy.openai_base_url`, `ctx.proxy.anthropic_base_url` and `ctx.proxy.token` work
  with the official SDKs and with frameworks built on them (tested: OpenAI and Anthropic
  SDKs for Python and Node, and PydanticAI's OpenAI and Anthropic models).
- `ctx.proxy.env()` returns `OPENAI_BASE_URL`, `OPENAI_API_KEY`, `ANTHROPIC_BASE_URL` and
  `ANTHROPIC_API_KEY` for subprocesses you start.
- HTTP agents receive the same binding in the run request (`proxy` field of
  `runweave.agent/v1`); the Python wrapper and the TypeScript SDK expose it as `ctx.proxy`.

`runweave serve` advertises `http://127.0.0.1:<port>`; set `RUNWEAVE_PROXY_PUBLIC_URL` if
agents reach RunWeave at another address.

## Tokens

- A token is issued when a run attempt starts and revoked when it finishes or its deadline
  passes. Requests without a live token get 401 (`PROXY_TOKEN_INVALID`) in the provider's
  error format. RunWeave stores only a hash of each token.
- Tokens are sent as `Authorization: Bearer <token>` (OpenAI SDKs) or `x-api-key` (Anthropic
  SDKs). They grant model calls for that one run and nothing else.

## Mock mode

Without fixtures every call answers `This is a deterministic RunWeave mock response.`
Script replies with a fixture file (`RUNWEAVE_PROXY_MOCK_FIXTURES=path/to/fixtures.json`,
checked at startup); the first matching rule wins:

```json
{
  "version": 1,
  "rules": [
    {"when": {"last_role": "tool"}, "reply": {"text": "It is sunny in Paris."}},
    {"when": {"contains": "weather", "has_tools": true},
     "reply": {"tool_calls": [{"name": "lookup_weather", "arguments": {"city": "Paris"}}]}},
    {"when": {"contains": "hello", "api": "messages"}, "reply": {"text": "Hello from Claude's stand-in."}}
  ]
}
```

| Condition | Matches when |
| --- | --- |
| `contains` | The last user message contains this text (case-insensitive) |
| `last_role` | `"user"`, or `"tool"` when the last message is a tool result |
| `has_tools` | The request declares tools (or does not) |
| `api` | `"chat"`, `"responses"` or `"messages"` |

Usage is estimated at four bytes per token. The same request gives the same reply and
usage; IDs are derived from the run, so they repeat across identical runs.

## Budgets

Each call reserves budget before it is answered: the request size plus framing as input
tokens, and `max_tokens` (or `RUNWEAVE_PROXY_DEFAULT_MAX_TOKENS`, default 1024) as output,
capped by what the run has left. It then settles the reported usage. A stream that is cut
short keeps its full reservation, because its usage is unknown. When a run's budget
(`RUNWEAVE_AGENT_REQUEST_LIMIT`, token and cost limits) is spent, calls get 402 with the
budget code (for example `RUN_BUDGET_EXCEEDED`) and nothing is answered. SDKs do not retry
402.

## Timeline and privacy

Each call adds a **Model call** step: the API, `mock`, `stream` if streamed, and the token
usage, or why it was blocked. Prompts and completions are never logged, stored or shown;
only sizes and counts are. Request bodies are limited to `RUNWEAVE_PROXY_REQUEST_MAX_BYTES`
(1 MiB by default).

## Limits

- The proxy is in-process: in durable mode, a run's token works on the RunWeave process
  executing it.
- Mock replies ignore the requested model name.
- Not yet: passthrough to real providers with your API key, seeded fault profiles, and the
  Settings screen (milestone 21, pull requests B and C).
