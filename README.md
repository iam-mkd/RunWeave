# RunWeave

An async orchestration runtime for AI agents, built with FastAPI and asyncio.
The foundation provides typed contracts, replaceable model/tool interfaces, and
an offline incident-triage demo. Milestone 2 adds bounded workers, a run queue,
submission/status/cancellation APIs, and bounded in-memory history. Milestone 3
adds execution deadlines, retries for the read-only health tool, and graceful draining.
Milestone 4 adds a model-driven tool loop and an optional Groq adapter.
Milestone 5 adds metrics, JSON logs, traces, and reproducible load evidence.

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

## PydanticAI workflow preview

The PydanticAI workflow is opt-in while running parity checks. Both workflows
use the same run API and provider settings. The preview uses PydanticAI for
structured decisions and Pydantic Graph for step routing. The existing Groq
adapter still owns the HTTP transport, request rate, and concurrency limits.

```powershell
uv sync --locked --extra workflow
$env:RUNWEAVE_AGENT_WORKFLOW = 'pydanticai'
uv run --no-sync pytest tests/test_runs_api.py tests/test_groq.py -q
uv run --no-sync python -m benchmarks.load --workers 100
```

The benchmark report includes `"agent_workflow": "pydanticai"` when this
workflow is selected.

If you also use PostgreSQL mode, restore its driver and migration CLI after
the locked sync with `uv pip install -r requirements-durable.txt`. Use
`uv run --no-sync` for subsequent commands so both sets of packages are
available.

For an opt-in live smoke test, set the Groq API key in `.env` and run one
synthetic incident. This makes live provider requests.

```powershell
$env:RUNWEAVE_MODEL_PROVIDER = 'groq'
uv run --no-sync python -m runweave.demo --service payments --description "Elevated latency"
```

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
| `telemetry.py` | Metrics, JSON run events, and OpenTelemetry spans |
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

## Observability and load testing

Scrape `GET /metrics` to monitor admission, active runs, queue depth, retries,
outcomes, and latency histograms. Set `RUNWEAVE_JSON_LOGS=true` for JSON run
events on stderr and `RUNWEAVE_CONSOLE_TRACES=true` for OpenTelemetry spans on
stdout. Run IDs correlate logs and spans. Metrics use bounded status and error
categories. The application emits only allowlisted telemetry fields and omits
incident descriptions, tool arguments, headers, and exception messages.

Run the offline benchmark from the repository root. It uses HTTPX's in-process
ASGI transport and a synthetic workload, with the fake model selected explicitly.

```powershell
uv run --locked python -m benchmarks.load
uv run --locked python -m benchmarks.load --workers 128 --soak-seconds 600
```

The benchmark commands, fault matrix, and measured results are in the
[benchmark report](benchmarks/README.md).

## Docker

```powershell
docker build -t runweave:local .
docker run --rm -p 127.0.0.1:8000:8000 runweave:local
```

The container runs one Uvicorn process and defaults to the fake model. Check readiness
at `http://127.0.0.1:8000/health/ready` and metrics at
`http://127.0.0.1:8000/metrics`.

## Verification

```powershell
uv run --locked pytest
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked mypy
```

All 62 tests passed in a temporary in-process harness that disabled the Windows
event loop's wakeup sockets for the test process. This exercised ASGI and mock
HTTP transports, including 128 overlapping mock runs. The sandbox still hangs
creating the native loop's socket pair. A normal local test run and Uvicorn
startup and shutdown remain pending. Groq was tested with mocked HTTP responses.
A live smoke test requires a key. Ruff, strict mypy, and the lockfile check pass.

## Current boundaries

The incident workflow uses synthetic service health and runbook evidence.
Shutdown's grace period bounds draining. Final cleanup depends on adapters
cooperating with cancellation. The default in-memory mode keeps run state for
the process lifetime, subject to retention limits.

Follow [PLAN.md](PLAN.md) in milestone order and [AGENTS.md](AGENTS.md) for coding
rules.

## PostgreSQL durable mode

Set `RUNWEAVE_DATABASE_URL` in `.env` to a dedicated PostgreSQL database using
`postgresql+asyncpg://user:password@127.0.0.1:5432/runweave`. Use
`RUNWEAVE_MODEL_PROVIDER=fake` to try recovery locally with synthetic evidence.
Install the optional driver and migration CLI after syncing the base environment,
then apply the migration and start the service.

```powershell
uv sync --locked
uv pip install -r requirements-durable.txt
uv run --no-sync alembic upgrade head
uv run --no-sync uvicorn runweave.app:create_app --factory --workers 1
```

Use the existing `POST /v1/runs`, `GET /v1/runs/{id}`, and cancel routes. Keep the
run ID across a process restart. PostgreSQL stores the incident, result, and
attempt history until retention cleanup. Unfinished jobs count toward
`RUNWEAVE_QUEUE_CAPACITY` across replicas. The hard row limit is queue capacity
plus history capacity. Terminal rows are pruned in batches.

Workers renew their leases every third of the configured lease interval. If a
worker exits, another process can claim the job after lease expiry and the
persisted retry delay. Recovery starts a new attempt from the beginning within
the original run deadline. This is at-least-once execution, so repeated attempts
can repeat inference. The provided tools are read-only. Write tools would need
downstream idempotency before they could be used safely in durable mode.

Shutdown stops claiming queued work and gives active work the configured grace
period. Unfinished claims can recover after their leases expire. A cancellation
committed by another replica reaches the executing worker on its next heartbeat.
Only the current lease can commit a result.

The integration test creates and removes a uniquely named schema. Point it at a
disposable database with schema creation permissions. It forces a worker process
to exit after claiming work, then tests recovery, competing claims, and stale writes.

On Windows, run this from a regular PowerShell session to install the test
dependencies, start a temporary PostgreSQL cluster on a free loopback port, run
the test, and clean up the cluster. Pass `-PostgresBin` if PostgreSQL is installed
in a different directory.

```powershell
.\scripts\verify-postgres.ps1
```

For an existing disposable database, use its connection URL instead.

```powershell
$env:TEST_RUNWEAVE_POSTGRES_URL = 'postgresql+asyncpg://user:password@127.0.0.1:5432/runweave_test'
uv run --no-sync pytest tests/test_postgres_recovery.py -q
```
