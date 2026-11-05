# RunWeave

An async orchestration runtime for AI agents, built with FastAPI and asyncio.
Milestone 1 provides application lifecycle, typed contracts, replaceable model/tool
interfaces, and a deterministic offline incident-triage demo.

## Quick start (PowerShell)

Install Python 3.13 and uv, then from this folder:

```powershell
uv sync --locked
uv run --locked runweave-demo
uv run --locked uvicorn runweave.app:create_app --factory --workers 1
```

Visit http://127.0.0.1:8000/docs for OpenAPI. Health endpoints:

- `GET /health/live`: process responds.
- `GET /health/ready`: application runtime is initialized and its shared client is open.

The demo prints a structured summary, severity, and `mock-health:payments` evidence
reference. Customize the synthetic incident:

```powershell
uv run --locked runweave-demo --service disputes --description "Elevated response latency"
```

No API key, external network request, or paid model is used. The shared async HTTP
client uses HTTPX MockTransport and a fixed synthetic destination; fake tool data
always reports degraded health. This is a contract/lifecycle demo, not a latency or
concurrency benchmark. MockTransport does not exercise real sockets or pool limits.

## Configuration

Settings load from `RUNWEAVE_` environment variables and an optional `.env`. Defaults
work out of the box. `.env.example` lists supported options; copy it to `.env` if
needed. Timeout and connection limits are validated at startup. Unrecognized keys
in `.env` fail validation to catch typos. `.env` is excluded from version control.

## Navigation and design

Application code lives in `src/runweave/`.

| Location | Responsibility |
| --- | --- |
| `app.py` | FastAPI factory and application lifespan |
| `core/settings.py` | Validated configuration |
| `core/runtime.py` | Construct implementations and own the shared HTTP client |
| `api/health.py` | Health HTTP contracts and routes |
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

Initial verification in the managed Windows environment passed all 11 synchronous
validation tests, Ruff, and strict mypy. Seven async tests and the demo are
pending: the environment hangs creating
the built-in event loop's local socket pair, before application code runs.
The dependency lockfile is now included and was checked offline after the
RunWeave rename. Run `uv sync --locked` to refresh an existing environment with
the renamed package and `runweave-demo` command. See PLAN.md for the remaining
Milestone 1 acceptance checks.

## Current boundaries

The runner has a fixed plan/check/summarize sequence, not an autonomous tool loop.
There are no job submission/polling APIs, queue, concurrency admission limits,
retries, deadlines, persistent state, or live model integration yet. Cancellation
propagates naturally to awaited tool calls; scheduler cancellation arrives later.
Readiness will incorporate scheduler health in milestone 2.

Follow [PLAN.md](PLAN.md) in milestone order and [AGENTS.md](AGENTS.md) for coding
rules. Next: bounded orchestration and run state tracking in milestone 2.
