# Run your own Python agent

To run your agent in its own process or in another language, see
[HTTP agents](http-agents.md).

RunWeave runs your agent with bounded concurrency, a deadline, cancellation, a step
timeline, input/output validation and run history. Write an async function, describe its
input and output with Pydantic models, and start RunWeave with it.

## 1. Declare the agent

```python
from pydantic import BaseModel
from runweave.sdk import RunContext, agent


class Question(BaseModel):
    text: str


class Answer(BaseModel):
    reply: str


@agent(name="my-agent", version="1.0.0", input=Question, output=Answer, max_steps=10)
async def run(input: Question, ctx: RunContext) -> Answer:
    async with ctx.step("Call the model", detail="draft answer"):
        reply = await my_async_model_call(input.text)  # Your own code and clients.
    ctx.log("answered")
    return Answer(reply=reply)
```

Rules the SDK enforces:

- The function must be `async def`. Blocking calls stall every run sharing the event
  loop; use async clients, or `asyncio.to_thread` for unavoidable blocking I/O.
- `name` uses lowercase letters, digits and hyphens (max 63). `incident-triage` is
  reserved for the built-in example agent.
- Input and output are Pydantic models. Input is validated before admission; output is
  validated when the function returns.

`RunContext` gives the agent:

| Member | Meaning |
| --- | --- |
| `ctx.run_id`, `ctx.agent`, `ctx.attempt` | Identity of this run. `attempt` is 2+ when durable mode re-runs it after a lost lease. |
| `ctx.remaining_seconds()` | Time left before the run deadline, which includes queue wait. |
| `ctx.step(title, detail=None)` | Async context manager that adds a timeline step; exceeding `max_steps` fails the run with `AGENT_STEP_LIMIT`. |
| `ctx.log(message)` | Logs to the `runweave.agent` logger with configured secrets redacted and a 1,000-character limit. |
| `await ctx.usage()` | The run's budget summary, when a budget is installed. |
| `ctx.proxy` | The run's [LLM proxy](llm-proxy.md) binding: `token`, `openai_base_url`, `anthropic_base_url` and `env()`. Use it as your model SDK's API key and base URL. |

Step titles and details are shown in the workspace and API, so keep secrets and raw
customer data out of them. Configured protected values are redacted automatically.

## 2. Start RunWeave with it

The agent's module must be importable in the environment that runs RunWeave (install your
package, or run from its directory):

```bash
runweave serve --agent mypkg.agent:run
```

Repeat `--agent` for more agents, or set `RUNWEAVE_AGENTS='["mypkg.agent:run"]'`.
`runweave serve --check --agent mypkg.agent:run` imports the agents, lists them and exits;
use it to catch import errors quickly. Agents are loaded only at startup; the server never
imports code because of an HTTP request.

Try the included example:

```bash
uv run --locked runweave serve --demo --agent sample_agent:text_report
```

with `examples/sample-agent` on `PYTHONPATH` (or `uv pip install -e examples/sample-agent`).

## 3. Submit and follow runs

```bash
curl -X POST http://127.0.0.1:8000/v1/runs -H "Content-Type: application/json" \
  -d '{"agent": "text-report", "input": {"text": "Hello there. Bye.", "keywords": ["hello"]}}'
```

| Endpoint | Behavior |
| --- | --- |
| `GET /v1/agents` | Registered agents with their input/output JSON Schemas |
| `POST /v1/runs` | `{"agent": name, "input": {...}}`; the legacy incident body still runs `incident-triage` |
| `GET /v1/runs?agent=&status=&limit=&before=` | Your runs, newest first (limit 1–100) |
| `GET /v1/runs/{id}` | Snapshot with `agent`, `agent_version`, `result`, `steps`, `usage`, `error` |
| `GET /v1/runs/{id}/events` | Server-sent events: a `snapshot` event per change until the run finishes |
| `POST /v1/runs/{id}/cancel` | Cancels the run; cancellation reaches your `finally` blocks |

Errors use stable codes: `UNKNOWN_AGENT` and `INVALID_INPUT` (422; locations and
messages only, never your submitted values), `INPUT_TOO_LARGE` (413), `QUEUE_FULL` (503),
`RUN_DEADLINE_EXCEEDED` (timed out), `AGENT_OUTPUT_INVALID`, `AGENT_OUTPUT_TOO_LARGE`,
`AGENT_STEP_LIMIT`, and `RUN_FAILED` for any other exception. Exception messages and
tracebacks are never returned.

Event streams close after `RUNWEAVE_EVENT_STREAM_MAX_SECONDS` (default 60) with a
`reconnect` event, send keep-alive comments while quiet, and are capped by
`RUNWEAVE_EVENT_STREAM_MAX_SUBSCRIBERS` (503 `EVENT_STREAMS_BUSY` beyond it).

## Limits and guarantees

| Setting | Default | Meaning |
| --- | --- | --- |
| `RUNWEAVE_RUN_REQUEST_MAX_BYTES` | 65536 | Request body limit for `POST /v1/runs`, enforced before parsing |
| `RUNWEAVE_AGENT_OUTPUT_MAX_BYTES` | 65536 | Largest serialized agent output |
| `RUNWEAVE_RUN_TIMEOUT_SECONDS` | 30 | Overall deadline from admission |
| Timeline | 200 events | Storage cap per run; later events are dropped |

- **Your code is trusted.** RunWeave bounds and observes your agent but does not sandbox
  it. It runs in the RunWeave process with that process's permissions.
- **In-memory mode** (default) loses runs on restart.
- **Durable mode** (PostgreSQL) stores input, output and the timeline. If a worker dies,
  the run is re-run from the beginning after its lease expires (`ctx.attempt` increases):
  at-least-once execution. Make side effects idempotent. Checkpoints, approvals and
  scoped memory remain features of the built-in triage workflow for now.
- If you redeploy an agent whose new input model rejects queued input, those runs fail
  with `AGENT_INPUT_INCOMPATIBLE`; runs for an agent that is no longer registered fail
  with `AGENT_UNAVAILABLE`.
