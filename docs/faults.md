# Fault catalog

Real providers rate-limit, fail, stall and sometimes return broken tool calls. RunWeave's
[LLM proxy](llm-proxy.md) can do the same on purpose, so you can see how your agent copes
before your users do. Faults apply only to model calls made through `ctx.proxy`.

## Choosing faults

- **For a project's commands:** `[proxy] fault_profile = "flaky"` in `runweave.toml`, or
  `runweave bench --fault-profile "" --fault-profile chaos` to compare without and with
  faults in one report.
- **For a running server:** the workspace's **Settings** screen, or
  `RUNWEAVE_PROXY_FAULT_PROFILE=chaos`.
- **Your own profiles:** a JSON file named by `RUNWEAVE_PROXY_FAULT_PROFILES_FILE` (checked
  at startup), or `RUNWEAVE_PROXY_FAULT_PROFILE=custom` with a list of faults in
  `RUNWEAVE_PROXY_FAULT_INLINE`:

```json
{"version": 1, "profiles": [
  {"name": "my-provider", "description": "Rate limits on Claude only",
   "faults": [{"kind": "rate_limit", "probability": 0.2, "retry_after": 2, "apis": ["messages"]}]}
]}
```

A profile's probabilities add up to at most 1. For each proxied call, one draw from the
seed (`fault_seed`, `RUNWEAVE_PROXY_FAULT_SEED`) and the call's position in the run picks at
most one fault that applies to that call, so the same seed and the same calls give the same
faults. `response_lost` is different: it acts on an agent's own side-effecting calls made
through `ctx.effect`, never on model calls, and is drawn on a separate counter, so adding
effects never changes which model calls fail. Faults apply in mock and passthrough mode, and errors use each provider's own format,
so SDK retry logic behaves as it would against the real API. `apis` limits a fault to `"chat"`, `"responses"` or `"messages"`.

Each injected fault is shown on the run's timeline (`Injected fault: rate_limit`) and
counted in the run's details and in `bench` reports.

## Faults

The tables are generated from the code by `scripts/reference-docs.py`.

<!-- generated:faults -->
| Kind | What the agent sees, and the run's budget |
| --- | --- |
| `rate_limit` | HTTP 429 with `Retry-After` (`retry_after` seconds). Not charged. |
| `server_error` | An HTTP 5xx (`status`, default 503). Not charged. |
| `timeout` | The call is held `seconds` (never past the run's deadline), then 504. Not charged. |
| `slow_first_token` | The answer starts `seconds` late, then completes. Charged as usual. |
| `stream_disconnect` | A stream drops after `after_chunks` events; streaming calls only. The full reservation is kept. |
| `malformed_tool_call` | A tool call whose JSON arguments are cut off; requests with tools only. Charged as usual. |
| `unknown_tool` | A tool call naming a tool the request did not declare; requests with tools only. Charged as usual. |
| `context_length` | HTTP 400 `context_length_exceeded` (OpenAI) or `prompt is too long` (Anthropic). Not charged. |
| `response_lost` | Effects only (`ctx.effect`): the write commits, then the response is dropped and the agent gets `EffectResponseLost`, a timeout. `effects` limits it to named effects. Never applies to model calls. |

### Built-in profiles

| Profile | What it does | Faults (kind, probability, parameters) |
| --- | --- | --- |
| `flaky` | Occasional rate limits, 503s and slow first tokens. | `rate_limit` 0.1, retry_after 1; `server_error` 0.05, status 503; `slow_first_token` 0.05, seconds 2 |
| `rate-limited` | Three in ten calls are rate limited. | `rate_limit` 0.3, retry_after 1 |
| `outage` | Every call fails with 503. | `server_error` 1 |
| `slow` | Slow first tokens and some timeouts. | `slow_first_token` 0.5, seconds 3; `timeout` 0.1, seconds 60 |
| `broken-tools` | Malformed tool-call JSON and unknown tool names. | `malformed_tool_call` 0.3; `unknown_tool` 0.2 |
| `chaos` | A little of everything. | `rate_limit` 0.1; `server_error` 0.08, status 502; `timeout` 0.04, seconds 30; `slow_first_token` 0.08, seconds 2; `stream_disconnect` 0.08; `malformed_tool_call` 0.08; `unknown_tool` 0.06; `context_length` 0.04 |
| `lost-writes` | Half of all effects commit their write, then lose the response. | `response_lost` 0.5 |
<!-- /generated:faults -->

## What a good agent does

| Fault | A resilient agent |
| --- | --- |
| `rate_limit`, `server_error` | Retries with backoff, honouring `Retry-After`, within its deadline. Most SDKs do this by default |
| `timeout`, `slow_first_token` | Sets a client timeout shorter than the run's deadline, and gives up cleanly |
| `stream_disconnect` | Treats a cut stream as a failed call, not as a complete answer |
| `malformed_tool_call`, `unknown_tool` | Validates tool calls before running them and asks the model again, or fails the run with a clear error |
| `context_length` | Trims context before calling, rather than retrying the same request |
| `response_lost` | Retries a write with the same idempotency key, so the downstream returns the first result instead of writing again. Check it with `exactly_once` ([effects](agents.md#effects-and-exactly-once-writes)) |

`runweave bench` reports failure categories per profile, so you can see which of these your
agent handles.
