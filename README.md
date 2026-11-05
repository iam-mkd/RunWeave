# RunWeave

An async orchestration runtime for AI agents, built with FastAPI and asyncio.
The foundation provides typed contracts, replaceable model/tool interfaces, and
an offline incident-triage demo. Milestone 2 adds bounded workers, a run queue,
submission/status/cancellation APIs, and bounded in-memory history. Async runtime
verification is still pending in the managed execution environment.

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

Runs move from `QUEUED` to `RUNNING`, then `SUCCEEDED`, `FAILED`, or `CANCELLED`.
`TIMED_OUT` is reserved for milestone 3. Repeated cancellation is idempotent;
cancelling a completed run returns its existing terminal state and result.
Cancellation records the state immediately and requests cooperative task
cancellation; the worker slot is released only after execution cleanup completes.

Saturated admission returns 503 with `Retry-After: 1` and code `QUEUE_FULL`.
An unavailable scheduler returns 503 with code `SCHEDULER_UNAVAILABLE`. Saturation
alone does not fail readiness. Unknown/expired IDs return 404 (`RUN_NOT_FOUND`);
malformed IDs or invalid request data return 422. An execution failure records
`RUN_FAILED` without exposing exception text. Failed runs can still be polled
with HTTP 200: the execution outcome lives in the returned run state.

The demo prints a structured summary, severity, and `mock-health:payments` evidence
reference. Customize the synthetic incident:

```powershell
uv run --locked runweave-demo --service disputes --description "Elevated response latency"
```

No API key, external network request, or paid model is used. The shared async HTTP
client uses HTTPX MockTransport and a fixed synthetic destination; fake tool data
always reports degraded health. This is a contract/lifecycle demo, not a latency or
concurrency benchmark. The mock tool awaits a 10 ms synthetic delay so work can
overlap. MockTransport does not exercise real sockets or connection-pool limits.

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

The oldest terminal records are evicted when the history cap is exceeded; TTL
expiration also applies on reads and submissions. Active/queued records are
not evicted by history limits. Cancelled queue entries remain until consumed
and skipped, so queued cancellation does not immediately free a queue slot.
All state is local to one process and is lost on restart. Run with one Uvicorn
worker. Shutdown currently cancels outstanding work and awaits cooperative cleanup;
the bounded graceful-drain policy belongs to milestone 3.

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
| `agents/contracts.py` | Incident schemas and model protocol |
| `agents/runner.py` | Coordinate one model → tool → summary run |
| `agents/fake.py` | Deterministic model adapter |
| `tools/contracts.py` | Health schemas and tool protocol |
| `tools/fake.py` | Offline HTTP tool and mock response |
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

Verification in the managed Windows environment passed all 17 synchronous
validation/state tests, Ruff, and strict mypy. Async tests and the demo remain
pending: the environment hangs creating the built-in event loop's local socket
pair, before application code runs. The async suite includes a barrier test for
128 overlapping runs, admission limits, cancellation races, failure isolation,
supervision, shutdown cleanup, retention cleanup, and API responses. These are
test scenarios awaiting execution, not measured performance claims.
The dependency lockfile is now included and was checked offline after the
RunWeave rename. Run `uv sync --locked` to refresh an existing environment with
the renamed package and `runweave-demo` command. See PLAN.md for the remaining
Milestone 1 and 2 acceptance checks.

## Current boundaries

The runner has a fixed plan/check/summarize sequence, not an autonomous tool loop.
Retries, overall execution deadlines, graceful draining, persistent state, and
live model integration are not implemented yet. A cooperative async tool can be
cancelled; Python cannot force-stop an adapter that suppresses cancellation or
blocks the event loop. No crash recovery or exactly-once execution is claimed.

Follow [PLAN.md](PLAN.md) in milestone order and [AGENTS.md](AGENTS.md) for coding
rules. Next after verification: recovery policies and lifecycle hardening in milestone 3.
