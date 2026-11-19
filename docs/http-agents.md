# Connect an agent over HTTP

Run your agent as its own process, in any language, and let RunWeave call it over HTTP.
RunWeave applies the same deadline, cancellation, bounded concurrency, validation, step
timeline and run history as for [in-process Python agents](agents.md). Nothing is
imported into RunWeave; it only calls a URL you give it.

The wire format is the versioned contract `runweave.agent/v1`, published as OpenAPI 3.1
in [`contracts/agent-http-v1.openapi.json`](../contracts/agent-http-v1.openapi.json).

## 1. Serve your agent

### Python

Write the agent with the same `@agent` decorator as an in-process agent, then serve it:

```bash
runweave serve-agent mypkg.agent:run --port 9100
```

`serve-agent` binds to `127.0.0.1` only and prints
`RUNWEAVE_AGENT_READY http://127.0.0.1:9100` once it is listening (`--port 0` picks a
free port). To embed it in your own ASGI server, use
`runweave.sdk_http.create_agent_app(run)`. The `RunContext` your function receives has
the same members: `run_id`, `attempt`, `remaining_seconds()` (from the deadline RunWeave
sends), `step()` and `log()`. `usage()` returns `None` until the LLM proxy (milestone 21)
meters model calls.

Try it with the included example:

```bash
uv run --locked runweave serve-agent sample_agent:text_report --port 9100
```

with `examples/sample-agent` on `PYTHONPATH`.

### TypeScript (Node)

The `@runweave/agent` package in `sdk/typescript` (no runtime dependencies, Node 22.12+ or
24+) provides `defineAgent` and `serveAgents`:

```js
import { defineAgent, serveAgents } from '@runweave/agent';

const agent = defineAgent({
  name: 'word-count', version: '1.0.0', inputSchema, outputSchema,
  async run(input, ctx) {
    const words = await ctx.step('Split words', () => input.text.split(/\s+/));
    return { words: words.length };
  },
});
const server = await serveAgents([agent], { port: 9200 }); // loopback only
```

The context has `runId`, `attempt`, `signal` (aborted on cancellation or deadline; pass it
to your I/O), `remainingMs()`, `step(title, fn, { detail })`, `log()` and `usage()`. The
package is not on npm yet; install it from the repository:

```bash
npm --prefix sdk/typescript ci
npm --prefix sdk/typescript run build
npm --prefix examples/http-agent-typescript install
npm --prefix examples/http-agent-typescript start
```

See `sdk/typescript/README.md` and `examples/http-agent-typescript`.

### Other languages

Implement the three endpoints below. The shared contract suite
(`tests/test_http_contract_suite.py`, fixture spec in `tests/contract/README.md`) runs the
same checks against the Python and TypeScript SDKs; add a fixture to run it against yours.

## 2. Register it with RunWeave

At startup (kept for the server's lifetime, and the only option for shared deployments):

```bash
runweave serve --demo --http-agent http://127.0.0.1:9100
```

Repeat `--http-agent` or set `RUNWEAVE_HTTP_AGENTS='["http://127.0.0.1:9100"]'`.
`runweave serve --check --http-agent …` fetches the manifest, lists the agents and exits.
Startup fails with a clear code if an agent is unreachable or its manifest is invalid.

At runtime from the workspace: **Agents → Connect agent → HTTP endpoint**, enter the URL,
**Test connection** to see the agents it serves (and any name conflicts), then
**Register agents**. Runtime-registered agents have a **Remove** button on the Agents
screen. Registering the same URL again refreshes its agents.

At runtime from the API (kept until the server stops), with the `agents:manage` scope,
which the local workspace has:

| Endpoint | Behavior |
| --- | --- |
| `POST /v1/agents/http/check` `{"url"}` | Fetches the manifest and reports its agents (and name conflicts) without registering them |
| `POST /v1/agents/http` `{"url"}` | Registers every agent at that URL. Posting the same URL again refreshes them (for example after you change a schema) |
| `DELETE /v1/agents/{name}` | Removes an agent registered at runtime. Built-in, Python and startup agents cannot be removed (409 `AGENT_NOT_REMOVABLE`) |

Registration errors: `AGENT_URL_NOT_ALLOWED` (422), `AGENT_UNREACHABLE`,
`AGENT_REDIRECT_DENIED` and `AGENT_MANIFEST_INVALID` (502), `AGENT_NAME_CONFLICT` and
`AGENT_LIMIT_REACHED` (409; `RUNWEAVE_HTTP_AGENT_MAX_REGISTERED`, default 32).

## 3. The contract

| Endpoint | Purpose |
| --- | --- |
| `GET /runweave/v1/agents` | Manifest: `{"contract": "runweave.agent/v1", "agents": [{name, version, description, input_schema, output_schema, max_steps}]}` (1–16 agents) |
| `POST /runweave/v1/agents/{name}/runs` | Body `{run_id, attempt, input, deadline_ms, proxy, approvals_url}`, header `Idempotency-Key: <run_id>`. Answer 200 `{"output": {...}, "steps": [{title, detail, status, duration_ms}]}`. `proxy` (`{token, openai_base_url, anthropic_base_url}`) is the run's [LLM proxy](llm-proxy.md) binding; `approvals_url` (durable mode only) is where the agent [requests approvals](agent-approvals.md) |
| `POST /runweave/v1/agents/{name}/runs/{run_id}/cancel` | Best-effort cancellation; answer 202, or 404 for unknown runs |

Schemas are JSON Schema 2020-12 with local references only (`#/$defs/...`, as Pydantic
emits); `input_schema` must describe an object. RunWeave
validates input before admission (422 `INVALID_INPUT`, with locations and fixed messages,
never your submitted values) and validates output against `output_schema`.

Errors are `{"error": {"code": "..."}}`. In a 500 response, the codes `RUN_FAILED`,
`AGENT_STEP_LIMIT`, `AGENT_OUTPUT_INVALID`, `AGENT_DEADLINE_EXCEEDED` and
`APPROVAL_EXPIRED` (both time the run out), `APPROVAL_CANCELLED`,
`APPROVAL_CAPACITY_EXCEEDED` and `DURABLE_APPROVAL_REQUIRED` are reported to the run;
anything else becomes `AGENT_SERVER_ERROR`. Answer 503 with
`Retry-After` when you are busy.

### What RunWeave does on the wire

- **Destinations:** loopback only (`127.0.0.0/8`, `::1`, `localhost`) unless a host is
  listed in `RUNWEAVE_HTTP_AGENT_ALLOWED_HOSTS` at startup. URLs with credentials, a query
  or a fragment are rejected. Proxy environment variables are ignored.
- **Redirects:** never followed (`AGENT_REDIRECT_DENIED`).
- **Size:** responses over `RUNWEAVE_HTTP_AGENT_RESPONSE_MAX_BYTES` (default 1 MiB) are
  rejected while streaming (`AGENT_RESPONSE_TOO_LARGE`); the output must also fit
  `RUNWEAVE_AGENT_OUTPUT_MAX_BYTES`.
- **Retries:** only when the agent cannot have started the run (connection refused or
  connect timeout) or asked for it (429, 502, 503, 504, honoring `Retry-After`). At most
  `RUNWEAVE_HTTP_AGENT_RETRY_ATTEMPTS` (default 3) attempts with jittered exponential
  backoff, all within the run deadline and all with the same `Idempotency-Key`. When
  retries run out the run fails with `AGENT_UNAVAILABLE`.
- **Never retried:** a dropped connection or read failure after the request was sent
  (`AGENT_CONNECTION_LOST`), other 5xx (`AGENT_SERVER_ERROR`), 422
  (`AGENT_INPUT_REJECTED`), other 4xx (`AGENT_REQUEST_REJECTED`) and malformed 200
  responses (`AGENT_RESPONSE_INVALID`).
- **Deadline and cancellation:** `deadline_ms` is the time left in the run's deadline
  (which includes queue wait). When the run is cancelled or times out, RunWeave closes the
  request and sends the cancel call with a one-second limit.
- **Steps:** returned steps are added to the run timeline, redacted like in-process
  steps. More steps than the manifest's `max_steps` fail the run with `AGENT_STEP_LIMIT`.
- **Approvals:** with `approvals_url`, the run request may stay open while the agent waits
  for a person; RunWeave pauses the run deadline meanwhile, so the request has no separate
  read timeout. See [the approval callback](agent-approvals.md#http-agents-and-the-typescript-sdk).

### What the Python wrapper does for you

- Validates the envelope (400 `INVALID_REQUEST`) and the input (422 `INVALID_INPUT`),
  limits request bodies (413 `INPUT_TOO_LARGE`, 1 MiB by default) and concurrent runs
  (503 `AGENT_BUSY`, 32 by default).
- Runs a repeated `Idempotency-Key` once: concurrent repeats wait for the first run and
  later repeats get its result for ten minutes (256 results). Reusing a key for a
  different run is 409 `IDEMPOTENCY_CONFLICT`.
- Cancels your function when RunWeave sends the cancel call or closes the request, so
  cancellation reaches your `finally` blocks.
- Logs unexpected exceptions in your process and answers `RUN_FAILED` without details.

## Limits and guarantees

- **Your service is trusted.** RunWeave bounds and observes the calls, but your agent
  runs with its own permissions. Bind it to loopback, as `serve-agent` does.
- **Runtime registrations are in memory.** They are lost when RunWeave stops; use
  `--http-agent` or `RUNWEAVE_HTTP_AGENTS` for agents durable runs depend on. In durable
  mode each RunWeave process has its own registrations.
- **Durable mode** re-runs a run after a lost lease with the same `Idempotency-Key`
  (`attempt` increases). If the agent already finished it, the Python wrapper returns
  the stored result; otherwise the run executes again (at-least-once). Queued input that
  no longer matches the agent's schema fails with `AGENT_INPUT_INCOMPATIBLE`.
- **Timeline:** steps arrive with the final response, not live, in this contract version.
