# RunWeave

**Test your AI agents like you test the rest of your code.** Check their answers, load-test
them, break their model provider on purpose, and compare versions, all on your own machine,
offline, with no API key.

![RunWeave in 12 seconds: evaluate and load-test an agent from the CLI, start a run in the workspace, see every model and tool call, and compare load-test results with and without injected provider faults](docs/images/demo.gif)

## Why RunWeave

Agents are easy to demo and hard to trust. The same prompt can take a different path every
time, a provider can rate-limit you mid-run, a "small" prompt change can quietly break three
other cases, and nobody knows what happens when fifty users arrive at once. Unit tests do
not catch any of that, and finding out in production is expensive.

RunWeave gives you the missing test harness:

| You want to know | RunWeave gives you |
| --- | --- |
| Does it still give the right answers? | `runweave eval`: a dataset of cases with assertions (output, schema, tools called, latency, cost), pass-rate gates and baselines that catch regressions. A failed gate exits 1, so CI fails the pull request |
| Will it hold up under load? | `runweave bench`: concurrency sweeps with throughput, p50/p95/p99 latency, queue wait, event-loop lag and memory per level |
| What happens when the provider misbehaves? | Seeded [faults](docs/faults.md): rate limits, 5xx, timeouts, cut-off streams, broken tool calls, so you see whether your retries actually work |
| Is the new version at least as good? | `runweave shadow`: the old and new versions on the same cases, side by side, with promotion gates |
| What did the agent actually do? | A local workspace with a live timeline of every model call (with tokens and cost) and tool call |
| Can it do something risky without asking? | `ctx.request_approval(...)`: the run waits until a person approves those exact arguments (durable mode, with PostgreSQL) |

## Built for developers

- **Bring your agent as it is.** A Python function with a decorator, any HTTP service in any
  language, or your existing framework: [examples](docs/examples.md) for the OpenAI Agents
  SDK, Claude Agent SDK, LangGraph, plain `openai` and TypeScript.
- **Offline and deterministic by default.** Point your OpenAI or Anthropic SDK at RunWeave's
  [LLM proxy](docs/llm-proxy.md): replies are mocked from a fixtures file, so tests are free,
  fast and reproducible. Switch to your own key when you want real models, with a spend cap
  per run.
- **Made for CI.** Exit codes, JUnit, JSON and Markdown reports, and a [GitHub Actions
  job](examples/github-actions) that fails a pull request when the agent regresses.
- **Honest numbers.** Every report records the machine, settings and dataset digest, and says
  plainly that a mock benchmark is [not provider capacity](docs/limits.md).
- **Yours.** Runs on `127.0.0.1`, keeps prompts out of logs, never needs an account, and is
  open source under Apache-2.0.

It is also useful beyond the team that writes the agent: **reviewers** read the eval report
on the pull request, **QA** builds datasets of the cases that matter, and **whoever pays the
provider bill** sees tokens and estimated cost per run before anything ships.

## Get started

With Git, [uv](https://docs.astral.sh/uv/getting-started/installation/) and Node.js 22.12+:

```bash
git clone https://github.com/iam-mkd/RunWeave.git
cd RunWeave
uv sync
npm --prefix frontend ci
npm --prefix frontend run build
uv run runweave init my-agent
cd my-agent
uv run runweave eval
```

The [quickstart](docs/quickstart.md) walks through it in five minutes, then load-tests the
agent and runs it in the workspace.

## Documentation

The [documentation](docs/README.md) starts with the [quickstart](docs/quickstart.md) and
[concepts](docs/concepts.md), covers connecting agents ([Python](docs/agents.md),
[HTTP](docs/http-agents.md), the [LLM proxy](docs/llm-proxy.md)), the
[workspace](docs/workspace.md), [reports](docs/reports.md), [faults](docs/faults.md),
[approvals](docs/agent-approvals.md), [budgets and your own key](docs/budgets.md),
[limits](docs/limits.md) and [troubleshooting](docs/troubleshooting.md), and has
[CLI](docs/cli.md) and [configuration](docs/configuration.md) references.

## Test your agent

```bash
runweave init my-agent          # or --language typescript
cd my-agent
runweave eval                   # offline, no API key; exits 1 when a gate fails
runweave bench                  # concurrency sweep, fault profiles, optional soak
runweave shadow --candidate agent_v2:run   # compare a new version before you switch
```

`runweave eval` runs a dataset through your agent with built-in assertions (output, schema,
tools, canaries, latency, cost), custom Python checks and Pydantic Evals, writes JSON,
JUnit and Markdown reports, and compares against a saved baseline. `runweave bench` reports
throughput, p50/p95/p99 latency, queue wait, status-API latency, event-loop lag, memory,
failure categories and tokens per concurrency level and fault profile. `runweave shadow`
pairs two versions case by case and applies the milestone 16 promotion gates. Run
`runweave serve` in the project folder and the workspace's Load tests, Evals and Shadow
screens start the same commands, chart load tests live, and list every saved report. A
copy-ready GitHub Actions job ([examples/github-actions](examples/github-actions)) fails a
pull request that regresses the agent. See [Test your agent](docs/testing.md).

## Run your own agent

Decorate an async Python function with `@runweave.sdk.agent`, then start RunWeave with it:

```powershell
uv run --locked runweave serve --agent mypkg.agent:run
```

RunWeave validates its input and output, applies the run deadline and cancellation,
records a step timeline, and exposes it through `/v1/agents`, `/v1/runs` (submit, list,
poll, cancel) and a live `/v1/runs/{id}/events` stream. See the
[agent guide](docs/agents.md) and the example in `examples/sample-agent`.

Agents in their own process (any language) connect over HTTP with the versioned
`runweave.agent/v1` contract: `runweave serve --http-agent http://127.0.0.1:9100`, or
connect them from the workspace (Agents → Connect agent → HTTP endpoint). Serve Python
agents with `runweave serve-agent mypkg.agent:run`, and Node agents with the TypeScript SDK
in `sdk/typescript`. See [HTTP agents](docs/http-agents.md).

Before an action that matters, an agent can wait for a person:
`await ctx.request_approval("issue_refund", {"order": "A-17"})` returns the decision for
those exact arguments (durable mode). Decide in the workspace's Approvals screen or with
`runweave approvals`. See [Approvals for your agents](docs/agent-approvals.md).

Point your model SDK at RunWeave's LLM proxy (`ctx.proxy`) and every call is metered,
budgeted and shown on the run timeline. It speaks OpenAI Chat Completions, OpenAI
Responses and Anthropic Messages, streaming included, and answers deterministically
offline by default. See [LLM proxy](docs/llm-proxy.md).

Framework examples show the same agent with plain Python and the `openai` client, the
OpenAI Agents SDK, the Claude Agent SDK, LangGraph and TypeScript, each a project that
`runweave eval` and `runweave bench` run offline in CI. See [Examples](docs/examples.md).

## Backend quick start (PowerShell)

The final backend milestone adds [offline shadow comparison and release verification](docs/release.md),
including a durable restart/recall/approval demo and native/container startup checks.
See [architecture and operations](docs/architecture.md) for deployment, migration,
rollback and the public deployment gate. The React workspace builds on that backend.

Milestone 15 adds opt-in disposable Python and read-only Chromium tools. See the
[isolation guide](docs/isolation.md) for native verification, configuration,
API examples, limits and the controlled fixture scope. Both tools stay disabled
by default and verify the target Docker runtime before accepting work.

Install Python 3.11 or newer (3.11, 3.12, and 3.13 are tested) and uv, then from this folder:

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
RUNWEAVE_MODEL_INPUT_USD_PER_MILLION=<your_input_rate>
RUNWEAVE_MODEL_OUTPUT_USD_PER_MILLION=<your_output_rate>
RUNWEAVE_MODEL_PRICE_VERSION=<your_contract_or_price_revision>
```

Run `uv run --locked runweave-demo` to make a live inference request. The demo
sends the synthetic incident and tool evidence to Groq. The health check and
runbook lookup still use local fixtures. The runner validates the model's tool
name and arguments against the allowlist, restricts calls to the incident's
service, and requires the final result to cite both returned evidence IDs.

Replace the pricing placeholders with your model/tier rates in USD per million
tokens and a price revision. The [budget guide](docs/budgets.md) describes the
per-run cost estimate, reservations, and conservative missing-usage accounting.

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

## Automated agent evaluation

Milestone 8 adds versioned synthetic fixtures and offline Pydantic Evals reports
for both workflows, with deterministic regression gates in CI.

```powershell
uv sync --locked --extra workflow --extra eval
uv run --no-sync python -m runweave.evaluation.suite --workflow baseline --output target/evals/baseline.json
uv run --no-sync python -m runweave.evaluation.suite --workflow pydanticai --output target/evals/pydanticai.json
```

See the [evaluation baseline](evaluations/README.md) for fixture review, report
provenance, seeded regressions, and the scope of deterministic quality checks.

## Authentication and execution policy

Milestone 9 adds owner-scoped run APIs, persisted principals, API/tool scopes,
atomic tenant admission limits, and credential checks at execution boundaries.
The default `local` mode uses one fixed developer identity. Select `jwt` mode
and configure a trusted HS256 issuer, audience, and signing key for authenticated
deployments. Local multi-tenant tests can use the `test` identity provider.

Foreign runs return 404, missing API scopes return 403, and tenant quota
rejections return 429 with `Retry-After`. PostgreSQL deployments must apply
`alembic upgrade head` before starting this version. See the
[security setup and policy guide](docs/security.md) for configuration, migration,
JWT claims, audit events, and verification.

Durable mode resumes typed workflow checkpoints with pinned workflow/prompt/model
identity and fenced operation receipts. Migration 0004 fails unfinished older
runs that have no checkpoint; stop workers before upgrading. See the
[checkpoint and replay guide](docs/checkpoints.md) for compatibility, effect
reconciliation, and upgrade details.

Milestone 12 adds opt-in sandbox ticket creation with durable human approval.
Submit `create_ticket: true`, review `/v1/runs/{id}/approval`, then submit the
exact bound decision. Waiting releases the worker lease and has a separate hard
expiry. See the [approval guide](docs/approvals.md) for scopes, API examples,
revocation, and migration 0005.

Milestone 13 adds opt-in scoped memory backed by PostgreSQL. Explicitly save
committed health/runbook evidence through `/v1/memory`, then use `"memory": {}`
on a later run to recall private facts. Project sharing requires configured
membership. Recall is historical, untrusted data with bounded retention and
context use. See the [memory guide](docs/memory.md) for migration 0006, scopes,
provenance, deletion, and examples.

Milestone 14 adds `RUNWEAVE_AGENT_WORKFLOW=coordinated`: a fixed coordinator with
one scoped health specialist and one scoped runbook specialist. Independent
handoffs overlap within the shared budget and resume committed branches after
restart. See the [coordination guide](docs/coordination.md) for required scopes,
failure policy, checkpoint compatibility, and baseline quality/cost comparisons.

## Configuration

Milestone 10 adds shared model-request, input/output-token, context, and estimated
spend limits. Both workflows report aggregate `usage` in run snapshots. Durable
recovery retains charges from previous attempts. PostgreSQL deployments must
apply migration 0003. It fails unfinished legacy jobs whose prior usage is unknown
and preserves terminal results with unavailable usage. See the
[budget configuration and upgrade guide](docs/budgets.md).

Settings load from `RUNWEAVE_` environment variables and an optional `.env`. Defaults
work out of the box. `.env.example` lists supported options; copy it to `.env` if
needed. Timeout and connection limits are validated at startup. Unrecognized keys
in `.env` fail validation to catch typos. `.env` is excluded from version control.

Every setting, with its default and allowed range, is in the [configuration
reference](docs/configuration.md).

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
| `api/testing.py`, `jobs.py` | Testing jobs (the CLI in a child process) and saved reports for the workspace |
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

## Desktop app

The desktop app wraps the same workspace in a window for Windows, macOS and Linux. It runs
RunWeave beside it with its own PostgreSQL, so approvals and run history work without
setting up a database. Your Python agents run with your own interpreter. Releases attach
unsigned installers; see [Desktop app](docs/desktop.md) for installing, the first launch of
an unsigned app, and uninstalling.

## Docker

The service image carries the workspace UI, durable mode (PostgreSQL driver and
migrations) and the workflow extra. With Compose, RunWeave runs with PostgreSQL at
http://127.0.0.1:8000:

```powershell
Copy-Item compose.env.example compose.env   # then set POSTGRES_PASSWORD
docker compose --env-file compose.env up --build
```

Or the image alone, in memory:

```powershell
docker build -t runweave:local .
docker run --rm -p 127.0.0.1:8000:8000 runweave:local
```

The container runs one Uvicorn process and defaults to the fake model. Publish it on
`127.0.0.1` only: the workspace accepts the `127.0.0.1` host and its own origin only.
Readiness is at `http://127.0.0.1:8000/health/ready` and metrics at
`http://127.0.0.1:8000/metrics`. See [release and Compose](docs/release.md#service-image-and-compose).

## Verification

```powershell
uv sync --locked --extra workflow --extra eval
uv run --no-sync pytest
uv run --no-sync ruff check .
uv run --no-sync ruff format --check .
uv run --no-sync mypy
```

On 2026-11-18 (milestone 17) the offline suite passed on native Windows with Python
3.11, 3.12, and 3.13: 264 passed and 100 opt-in PostgreSQL/native-isolation cases
skipped, with Ruff, formatting, and strict mypy (targeting 3.11) clean. CI runs the
same checks on Linux and Windows for all three versions and audits Python and
frontend dependencies. The PostgreSQL cases run in the offline evaluation workflow
and through `scripts/verify-postgres.ps1`; the 86 database cases last passed on
disposable PostgreSQL 18 at milestone 16. Coverage includes
worker termination at checkpoint/effect boundaries for both workflows, approval
restart and sandbox-commit recovery, scoped memory across process restart,
provenance, deletion/expiry, memory injection boundaries, concurrent quotas,
authorization revocation, owner/project isolation, coordinated branch replay and
trace/cancellation/budget boundaries, and migration. All three offline
evaluation reports passed their 13 cases and 68 assertions. Ruff, strict mypy,
and the lockfile check passed. Groq coverage uses
mocked HTTP responses. Live inference requires explicit opt-in and a key.
Native Uvicorn startup/shutdown and the main container image were verified in
milestone 16 (see [the release guide](docs/release.md)). The paired coordination comparison preserves fixture quality with three model
requests per workflow and about one-third fewer conservatively estimated input
tokens. Its prices are synthetic and its timing samples are illustrative; see
[the comparison limits](docs/coordination.md#verification-and-comparison).

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

Migration 0007 adds agent identity and the step timeline to runs; existing rows become
`incident-triage` runs. Stop workers before upgrading.

Use the existing `POST /v1/runs`, `GET /v1/runs/{id}`, and cancel routes. Keep the
run ID across a process restart. PostgreSQL stores the incident, result, and
attempt history until retention cleanup. Unfinished jobs count toward
`RUNWEAVE_QUEUE_CAPACITY` across replicas. The hard row limit is queue capacity
plus history capacity. Terminal rows are pruned in batches.

Workers renew their leases every third of the configured lease interval. If a
worker exits, another process can claim the job after lease expiry and the
persisted retry delay. Recovery resumes the next saved workflow node within the
original run deadline. Committed results are reused; inference interrupted before
checkpoint commit can repeat and consume additional budget. Public tools are
read-only. The internal effect boundary requires downstream confirmation and
idempotency for retries, or fails for manual resolution of an unknown outcome.

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

## Contributing, security, and license

See [CONTRIBUTING.md](CONTRIBUTING.md) for setup, the test-first workflow, and
pull request checks. Report vulnerabilities privately as described in
[SECURITY.md](SECURITY.md). Changes are recorded in [CHANGELOG.md](CHANGELOG.md).

RunWeave is licensed under the [Apache License 2.0](LICENSE). Third-party
dependency licenses are summarized in [docs/licenses.md](docs/licenses.md).
