# RunWeave

An async orchestration runtime for AI agents, built with FastAPI and asyncio.
The foundation provides typed contracts, replaceable model/tool interfaces, and
an offline incident-triage demo. Milestone 2 adds bounded workers, a run queue,
submission/status/cancellation APIs, and bounded in-memory history. Milestone 3
adds execution deadlines, retries for the read-only health tool, and graceful draining.
Milestone 4 adds a model-driven tool loop and an optional Groq adapter.

## Quick start (PowerShell)

Install Python 3.13 and uv, then from this folder:

```powershell
uv sync --locked
uv run --locked runweave-demo
uv run --locked uvicorn runweave.app:create_app --factory --workers 1
```

Visit http://127.0.0.1:8000/docs for OpenAPI. Health endpoints:

- `GET /health/live`: process responds.
- `GET /health/ready`: shared client is open and the scheduler is accepting work.

## Run API

| Endpoint | Behavior |
| --- | --- |
| `POST /v1/runs` | Accept an incident; return 202, a run snapshot, and a Location header |
| `GET /v1/runs/{id}` | Return state, timestamps, result, or sanitized execution error |
| `POST /v1/runs/{id}/cancel` | Cancel queued/running work; return its current snapshot |

With the server running, submit and inspect a run from another terminal:

```powershell
$payload = @{ service = "payments"; description = "Elevated response latency" } | ConvertTo-Json
$run = Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/v1/runs -ContentType application/json -Body $payload
Invoke-RestMethod -Uri "http://127.0.0.1:8000$($run.status_url)"
Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8000$($run.status_url)/cancel"
```

Runs move from `QUEUED` to `RUNNING`, then `SUCCEEDED`, `FAILED`, `TIMED_OUT`, or
`CANCELLED`. Queued runs can time out or be cancelled before starting. Cancelling
a terminal run returns its existing snapshot. Cancellation records the state
immediately and asks the running task to stop. The worker slot is released after
execution cleanup completes.

Saturated admission returns 503 with `Retry-After: 1` and code `QUEUE_FULL`.
An unavailable scheduler returns 503 with code `SCHEDULER_UNAVAILABLE`. Saturation
alone does not fail readiness. Unknown/expired IDs return 404 (`RUN_NOT_FOUND`);
malformed IDs or invalid request data return 422. An execution failure records
`RUN_FAILED` without exposing exception text. A timed-out run reports
`RUN_DEADLINE_EXCEEDED` or `STEP_DEADLINE_EXCEEDED`. Polling a failed or timed-out
run returns HTTP 200 with the execution outcome in the snapshot.

The demo prints a structured summary, severity, and evidence IDs for synthetic
service health and a local runbook fixture. To customize the incident, run:

```powershell
uv run --locked runweave-demo --service disputes --description "Elevated response latency"
```

Fake mode runs offline. The health tool uses HTTPX MockTransport and a fixed
synthetic destination, and the runbook lookup uses a local fixture. The mock
health tool awaits a 10 ms delay so offline runs can overlap.

## Groq inference

Use the local `.env` file, or copy `.env.example` to `.env` if you are setting up
a fresh checkout. Replace the key placeholder and select Groq:

```dotenv
RUNWEAVE_MODEL_PROVIDER=groq
RUNWEAVE_GROQ_API_KEY=replace_with_your_groq_api_key
RUNWEAVE_GROQ_MODEL=llama-3.3-70b-versatile
```

Run `uv run --locked runweave-demo` to make a live inference request. The demo
sends the synthetic incident and tool evidence to Groq. The health check and
runbook lookup still use local fixtures. The runner validates the model's tool
name and arguments against the allowlist, restricts calls to the incident's
service, and requires the final result to cite both returned evidence IDs.

The default is `fake`. The `.env` file is ignored by Git. Groq requests use a
fixed HTTPS endpoint with redirects disabled. The completion-token budget
applies across model turns, and input and tool history are bounded by size and
step limits. The step deadline includes any wait for a provider slot or rate limit.

## Configuration

Settings load from `RUNWEAVE_` environment variables and an optional `.env`. Defaults
work out of the box. `.env.example` lists supported options; copy it to `.env` if
needed. Timeout and connection limits are validated at startup. Unrecognized keys
in `.env` fail validation to catch typos. `.env` is excluded from version control.

| Setting | Default | Meaning |
| --- | --- | --- |
| `RUNWEAVE_WORKER_COUNT` | 128 | Maximum simultaneously executing runs |
| `RUNWEAVE_QUEUE_CAPACITY` | 1000 | Maximum pending queue entries |
| `RUNWEAVE_HISTORY_MAX_RECORDS` | 1000 | Maximum retained terminal runs |
| `RUNWEAVE_HISTORY_TTL_SECONDS` | 3600 | Terminal record lifetime, starting at completion |
| `RUNWEAVE_HISTORY_CLEANUP_INTERVAL_SECONDS` | 30 | Background history pruning interval |
| `RUNWEAVE_RUN_TIMEOUT_SECONDS` | 30 | Total deadline from admission, including queue wait |
| `RUNWEAVE_STEP_TIMEOUT_SECONDS` | 5 | Wall-clock budget per model or tool step |
| `RUNWEAVE_SHUTDOWN_GRACE_SECONDS` | 5 | Time to drain accepted runs before cancelling remaining work |
| `RUNWEAVE_TOOL_RETRY_ATTEMPTS` | 3 | Maximum health lookup attempts, including the first |
| `RUNWEAVE_TOOL_RETRY_BASE_DELAY_SECONDS` | 0.1 | Initial exponential backoff ceiling |
| `RUNWEAVE_TOOL_RETRY_MAX_DELAY_SECONDS` | 2 | Maximum delay between health lookup attempts |
| `RUNWEAVE_AGENT_MAX_STEPS` | 7 | Maximum model and logical tool interactions per run |
| `RUNWEAVE_AGENT_COMPLETION_TOKENS` | 2048 | Cumulative model completion-token budget per run |
| `RUNWEAVE_GROQ_CONCURRENCY` | 2 | Groq request slots per process |
| `RUNWEAVE_GROQ_REQUESTS_PER_MINUTE` | 30 | Groq request starts per minute, evenly spaced |

The health lookup retries selected transient HTTP and transport failures with
exponential backoff and jitter. It observes `Retry-After` in seconds or HTTP-date
form. If the requested wait exceeds the configured cap or would use up the
remaining deadline, the failure is returned immediately. Attempts and backoff
share the step and run budgets. The worker keeps its slot during backoff.

The oldest terminal records are evicted when the history cap is exceeded; TTL
expiration also applies on reads and submissions. Active/queued records are
not evicted by history limits. Cancelled and timed-out queue entries remain
until a worker consumes and skips them.
All state is local to one process and is lost on restart. Run with one Uvicorn
worker. Shutdown stops admission, makes readiness fail, and lets accepted runs
finish within the grace period. It then cancels any remaining work and waits for
cooperative cleanup before closing the shared HTTP client.

## Navigation and design

Application code lives in `src/runweave/`.

| Location | Responsibility |
| --- | --- |
| `app.py` | FastAPI factory and application lifespan |
| `core/settings.py` | Validated configuration |
| `core/runtime.py` | Construct implementations and own the shared HTTP client |
| `api/health.py` | Health HTTP contracts and routes |
| `api/runs.py` | Submission, polling, and cancellation HTTP contracts |
| `api/dependencies.py` | Resolve the lifespan-owned scheduler for requests |
| `orchestration/scheduler.py` | Admission, workers, cancellation, supervision, cleanup task |
| `orchestration/registry.py` | Atomic in-memory transitions and history eviction |
| `orchestration/models.py` | Immutable run snapshots, states, and error contracts |
| `deadline.py` | Shared run and step deadline scopes |
| `agents/contracts.py` | Incident schemas and model protocol |
| `agents/runner.py` | Coordinate one model → tool → summary run |
| `agents/fake.py` | Deterministic model adapter |
| `agents/groq.py` | Groq tool-call protocol and provider request limits |
| `tools/contracts.py` | Health schemas and tool protocol |
| `tools/registry.py` | Allowlisted tools, validated arguments, and evidence |
| `tools/fake.py` | Offline HTTP tool and mock response |
| `tools/retry.py` | Transient failure classification and health lookup backoff |
| `demo.py` | Command-line entry point using the same runtime wiring |
| `tests/` | Lifecycle, validation, substitution, and cancellation checks |

Dependencies flow from API/composition into behavior and contracts. The runner
depends on small protocols and receives its collaborators through its constructor.
Adapters need no shared base class; replacing a tool does not require editing the
runner. No service locator, generic repository, or plugin framework is introduced.
Comments are reserved for non-obvious decisions; names and types explain routine code.

## Verification

```powershell
uv run --locked pytest
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked mypy
```

All 61 tests passed in a temporary in-process harness that disabled the Windows
event loop's wakeup sockets for the test process. This exercised ASGI and mock
HTTP transports, including 128 overlapping mock runs. The sandbox still hangs
creating the native loop's socket pair. A normal local test run and Uvicorn
startup and shutdown remain pending. Groq was tested with mocked HTTP responses.
A live smoke test requires a key. Ruff, strict mypy, and the lockfile check pass.

## Current boundaries

The incident workflow uses synthetic service health and runbook evidence.
Shutdown's grace period bounds draining. Final cleanup depends on adapters
cooperating with cancellation. Run state remains in memory for the process lifetime.

Follow [PLAN.md](PLAN.md) in milestone order and [AGENTS.md](AGENTS.md) for coding
rules. Milestone 5 adds observability and reproducible load evidence.
