# LLM proxy

RunWeave serves OpenAI- and Anthropic-compatible endpoints so your agent's model calls are
metered, budgeted and shown on the run timeline, without changing agent code beyond the
base URL and API key. By default it runs in **mock mode**: deterministic, offline and
free. **Passthrough mode** forwards to your provider with your key, and **seeded fault
profiles** inject the failures real providers produce, reproducibly, in either mode.

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

## Fault injection

Pick a profile with `RUNWEAVE_PROXY_FAULT_PROFILE` and a seed with
`RUNWEAVE_PROXY_FAULT_SEED`. For each proxied call, one draw derived from the seed and the
call's position in the run attempt selects at most one fault, so **the same seed and the
same sequence of calls reproduce the same faults**. Faults apply in mock and passthrough
mode.

| Fault | What the agent sees | Budget |
| --- | --- | --- |
| `rate_limit` | 429 with `Retry-After` (`retry_after` seconds) | Not charged |
| `server_error` | 5xx (`status`, default 503) | Not charged |
| `timeout` | The request is held `seconds` (never past the run deadline), then 504 | Not charged |
| `context_length` | 400 `context_length_exceeded` (OpenAI) or `prompt is too long` (Anthropic) | Not charged |
| `slow_first_token` | The answer starts `seconds` late, then completes | Charged as usual |
| `stream_disconnect` | A stream drops after `after_chunks` events (streams only) | Full reservation kept |
| `malformed_tool_call` | A tool call whose JSON arguments are cut off (requests with tools only) | Charged as usual |
| `unknown_tool` | A tool call naming a tool the request did not declare (requests with tools only) | Charged as usual |

Errors use each provider's own format, so SDK retry logic behaves as it would against the
real API. Tool-call faults are answered by RunWeave even in passthrough mode. Each faulted
call shows `fault: <kind>` in its Model call step.

Built-in profiles:

| Profile | Faults |
| --- | --- |
| `flaky` | 10% rate limits, 5% 503s, 5% slow first tokens |
| `rate-limited` | 30% rate limits |
| `outage` | Every call fails with 503 |
| `slow` | 50% slow first tokens, 10% 60-second timeouts |
| `broken-tools` | 30% malformed tool-call JSON, 20% unknown tool names |
| `chaos` | A little of everything (8 kinds) |

Add your own in a file (`RUNWEAVE_PROXY_FAULT_PROFILES_FILE`, checked at startup), or set
`RUNWEAVE_PROXY_FAULT_PROFILE=custom` with an inline list in `RUNWEAVE_PROXY_FAULT_INLINE`:

```json
{"version": 1, "profiles": [
  {"name": "my-flaky", "description": "Rate limits on Claude only",
   "faults": [{"kind": "rate_limit", "probability": 0.2, "retry_after": 2, "apis": ["messages"]}]}
]}
```

A profile's probabilities add up to at most 1. Faults that don't apply to a call (a stream
fault on a non-streaming call, a tool fault without tools) are skipped for that call.

## Passthrough to your provider

```bash
RUNWEAVE_PROXY_MODE=passthrough
RUNWEAVE_PROXY_OPENAI_API_KEY=<your OpenAI key>          # and/or
RUNWEAVE_PROXY_ANTHROPIC_API_KEY=<your Anthropic key>
RUNWEAVE_PROXY_MODELS='["gpt-4.1-mini","claude-sonnet-4-5"]'
RUNWEAVE_MODEL_INPUT_USD_PER_MILLION=<price>
RUNWEAVE_MODEL_OUTPUT_USD_PER_MILLION=<price>
RUNWEAVE_MODEL_PRICE_VERSION=<your price list date>
RUNWEAVE_AGENT_MAX_COST_USD=0.05                         # spend cap per run
```

- **Your key stays in RunWeave.** Agents only ever hold their run's proxy token; RunWeave
  replaces it with the provider key when forwarding. Keys are redacted from logs and never
  returned in responses.
- **Only allowlisted models are forwarded** (400 `MODEL_NOT_ALLOWED` otherwise), because
  the configured prices, and so the spend cap, apply to every forwarded model. Set prices
  for the most expensive model you allow. A provider without a key answers 400
  `PROVIDER_NOT_CONFIGURED`.
- **Budgets stop overspend before forwarding.** The output-token limit sent upstream is
  capped at the call's reservation. Reported usage is settled from the response or the
  stream (Chat Completions streams always ask the provider for usage; the extra usage
  chunk is removed if your client did not ask for it). Missing usage is charged at the
  full reservation.
- **Per-provider limits:** `RUNWEAVE_PROXY_OPENAI_CONCURRENCY` / `_REQUESTS_PER_MINUTE` and
  the `ANTHROPIC` equivalents (defaults 4 and 60) bound in-flight calls and the call rate
  independently, and waiting consumes the run's deadline.
- **Provider errors are relayed, not retried** (your SDK decides), with their status,
  body and `Retry-After`; other headers are dropped. Rejected calls are not charged.
  Unreachable providers answer 502 `UPSTREAM_UNAVAILABLE`; redirects are never followed
  (`UPSTREAM_REDIRECT_DENIED`); responses over `RUNWEAVE_PROXY_UPSTREAM_MAX_BYTES`
  (8 MiB) are cut off (`UPSTREAM_RESPONSE_TOO_LARGE`, charged at the reservation).
- `RUNWEAVE_PROXY_OPENAI_BASE_URL` and `RUNWEAVE_PROXY_ANTHROPIC_BASE_URL` (HTTPS only)
  point at other compatible providers or gateways.
- RunWeave's own tests exercise passthrough only against scripted upstreams; CI never
  calls a real provider.

## Limits

- The proxy is in-process: in durable mode, a run's token works on the RunWeave process
  executing it.
- Mock replies ignore the requested model name.
- Fault profiles and the seed are server-wide settings; the Settings screen (next pull
  request) changes them without editing environment variables. Choosing a profile per
  run is planned with the testing CLI (milestone 22).
