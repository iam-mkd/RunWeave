# Troubleshooting

Find the message you see, then the fix. RunWeave prints problems as one line, never a
traceback; API errors carry a stable `code`.

## Installing and starting

**`runweave: command not found`** (or "not recognized"). From a clone, run commands
through uv: `uv run runweave ...`, inside the clone or a folder below it. Elsewhere, install
RunWeave into the environment you use: `uv pip install -e path/to/RunWeave`.

**`npm ci` fails with an engine or syntax error.** The workspace needs Node.js 22.12 or newer
(or 24): check with `node --version`.

**`No built workspace UI in ...`** when starting `runweave serve`. Build it once in the clone:
`npm --prefix frontend ci`, then `npm --prefix frontend run build`. To serve only the API,
add `--api-only`.

**`[Errno 10048]`, `address already in use`.** Something else listens on port 8000, often an
earlier `runweave serve`. Stop it, or use `runweave serve --port 8001` and open that port.

**`Invalid host header`.** Open the workspace at `http://127.0.0.1:8000`, not `localhost` or
your machine's name: it accepts only that address.

**`Extra inputs are not permitted` naming a setting.** A line in `.env` (in the folder where
you started RunWeave) has a name RunWeave does not know, often a typo. Check it against the
[configuration reference](configuration.md#service-settings-runweave_). Environment
variables with unknown `RUNWEAVE_` names are ignored, so check those for typos too.

## Projects and evals

**`No runweave.toml here or in any parent folder`.** Run the command inside your project
folder, pass `--config path/to/runweave.toml`, or create one with `runweave init`.

**`runweave.toml: ...` followed by a field and a reason.** The project file has an unknown key
or a value out of range. The [`runweave.toml` reference](configuration.md#runweavetoml)
lists every key with its allowed values.

**`Agent module 'agent' could not be imported: ...`.** The rest of the message is Python's
error, such as a missing package or a syntax error. Install the agent's dependencies in the
same environment as RunWeave, and run from the folder that contains the module. `runweave
serve --check --agent agent:run` reports import problems without starting anything.

**`... is not decorated with @runweave.sdk.agent`.** The target must point at the decorated
function itself, as `module:function`.

**A case fails with `TIMED_OUT`.** It ran past `[limits] run_timeout_seconds` (30 by default).
Raise it if the agent is slow by design; otherwise look at the run's timeline for the slow
step.

**A case fails with `output is ..., expected ...`.** The agent answered, but not what the case
expects. If the new answer is right, update the case; [reading reports](reports.md#eval)
explains each assertion.

**Gates fail although the pass rate is fine.** A case that passed in the baseline fails now.
The summary lists it under "Regressed against the baseline". Fix it, or save a new
baseline with `runweave eval --save-baseline` if the change is intended.

## Runs and the API

| Code | HTTP | Meaning and fix |
| --- | --- | --- |
| `UNKNOWN_AGENT` | 422 | No agent by that name. `GET /v1/agents` lists them; Python agents load only at startup |
| `INVALID_INPUT` | 422 | The input does not match the agent's input schema; the error lists where |
| `INPUT_TOO_LARGE` | 413 | Over `RUNWEAVE_RUN_REQUEST_MAX_BYTES` (64 KiB) |
| `QUEUE_FULL` | 503 | The queue is at `RUNWEAVE_QUEUE_CAPACITY`. Retry after `Retry-After` seconds, or raise the capacity |
| `RUN_DEADLINE_EXCEEDED` | in the run | The run passed its deadline, including time in the queue |
| `AGENT_OUTPUT_INVALID` | in the run | The agent returned something its output model rejects |
| `AGENT_STEP_LIMIT` | in the run | More steps than the agent's `max_steps` allows |
| `RUN_FAILED` | in the run | The agent raised an exception. The message is not returned, for safety; look at the server's log |

## Model calls through the proxy

**`402` with `RUN_BUDGET_EXCEEDED`** (or another budget code). The run spent its model calls,
tokens or cost budget, so the proxy answered nothing. Raise the limit
([budgets](budgets.md)), or find the loop in the run's timeline that keeps calling the
model.

**Every model reply is `This is a deterministic RunWeave mock response.`** That is mock mode
without fixtures. Script replies and tool calls with `[proxy] mock_fixtures` ([LLM
proxy](llm-proxy.md#mock-mode)), or switch to your own key ([bring your own
key](budgets.md#bring-your-own-key)).

**`400 MODEL_NOT_ALLOWED`.** In passthrough mode only allowlisted models are forwarded. Add the
model to `RUNWEAVE_PROXY_MODELS` or on the Settings screen.

**`400 PROVIDER_NOT_CONFIGURED`.** No key for that provider. Add it on the Settings screen or
with `RUNWEAVE_PROXY_OPENAI_API_KEY` / `RUNWEAVE_PROXY_ANTHROPIC_API_KEY`.

**`KEY_STORE_UNAVAILABLE` when saving a key.** The machine has no credential store (for
example Linux without Secret Service). Set the key as an environment variable instead.

**My SDK fails at random during `bench`.** A fault profile is on. That is the point: see the
[fault catalog](faults.md) for what each fault looks like and how to handle it, or bench
with `--fault-profile ""` for a run without faults.

## Performance

**`Loop lag p99` of tens of milliseconds or more in a bench report.** Something blocks the
event loop, usually synchronous I/O or heavy computation in an agent. Use async clients,
`await asyncio.to_thread(...)` for unavoidable blocking calls, and a process pool for CPU
work.

**Throughput stops rising and queue wait climbs.** That concurrency level is past what the
agent (or this machine) serves at once. See [reading reports](reports.md#bench).

## Desktop app

**macOS: "RunWeave could not start: unknown path".** The app runs from a folder whose path
contains a symlink. Move it to Applications.

**Windows SmartScreen or macOS Gatekeeper blocks the app.** It is not signed yet. See [first
launch of an unsigned app](desktop.md#first-launch-of-an-unsigned-app).

Still stuck? Open an issue with the command, the full message and your OS, leaving out
keys and private data. Security problems go through [SECURITY.md](../SECURITY.md) instead.
