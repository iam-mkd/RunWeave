# RunWeave

## Purpose and current scope
Build a FastAPI service that runs 100+ concurrent I/O-bound agent runs with
bounded resource use, non-blocking tools, observable execution, and explicit
failure handling. Read PLAN.md before implementation.

Milestones 1 through 6 implementation are authorized. Work in small, reviewable milestones
and follow the accepted scope and completion criteria in PLAN.md.

## Proposed stack
- Python 3.13 baseline; FastAPI, Uvicorn, Pydantic v2, pydantic-settings.
- asyncio for scheduling, bounded queues, timeouts, and cancellation.
- One shared httpx.AsyncClient per application process.
- uv for environment management and locking; Ruff and mypy for static checks.
- pytest, pytest-asyncio, and HTTPX mock transports for deterministic tests.
- Prometheus metrics, structured JSON logs, OpenTelemetry traces.
- PostgreSQL and async SQLAlchemy/Alembic only in the durable milestone.
- Docker for packaging; local mock tools must work without paid credentials.

Resolve compatible dependency versions and commit the lockfile when scaffolding.
Use the built-in Windows asyncio loop; do not require uvloop.

## Architecture rules
- Separate API, scheduler, agent runner, tools, storage, and telemetry.
- An agent run is one independently tracked job; an agent step is one model or
  tool interaction within that job. Do not confuse jobs with server processes.
- MVP runs with one Uvicorn process. In-memory queues and registries are not
  shared across processes or replicas and do not survive crashes.
- Accept jobs with HTTP 202 only after admission succeeds. Queue saturation
  returns 503 with Retry-After; tenant quotas, if implemented, return 429.
- Bound queue depth, active runs, HTTP connections, per-provider concurrency,
  execution steps, token budget, input/output sizes, and retained job history.
- Create workers and shared resources in FastAPI lifespan; own, await, and
  clean up every background task. Do not use FastAPI BackgroundTasks as the
  orchestration engine.
- Supervise workers with structured concurrency. Catch and record expected
  per-run failures inside workers so one job does not cancel sibling workers.
  Unexpected scheduler failures must make readiness fail and be surfaced.
- Never hold registry/state locks across network calls or slow tool execution.

## Async and recovery rules
- No time.sleep, requests, synchronous model calls, or blocking file/DB calls
  inside async request handlers or runners. Use async clients for I/O.
- Use asyncio.to_thread only for unavoidable blocking I/O; cancellation of its
  awaiter does not terminate the underlying thread. CPU-heavy work belongs in
  a bounded process pool or separate worker service.
- Use queue.get and other real await points; never busy-poll.
- Track QUEUED, RUNNING, SUCCEEDED, FAILED, TIMED_OUT, and CANCELLED states with
  validated transitions. Terminal states cannot be overwritten by late results.
- Cancellation must propagate through tools and retry delays. Clean up in
  finally blocks and re-raise asyncio.CancelledError after cleanup.
- Enforce an overall job deadline including queue wait, plus step/tool limits.
  Retry delays and every retry attempt consume the same deadline budget.
- Retry only classified transient failures with bounded exponential backoff,
  jitter, and Retry-After handling. Do not retry invalid input or auth failures.
- Treat timeouts on side effects as ambiguous. Retry side effects only when
  the downstream operation supports a real idempotency key or deduplication.
- A semaphore bounds in-flight calls; a rate limiter bounds calls per interval.
  Configure both independently for live providers.
- Shutdown stops admissions, fails readiness, and drains for a bounded grace
  period. Cancel and await remaining work, then close clients and storage.
- Never promise crash recovery or exactly-once effects in the in-memory MVP.

## Agent and tool boundaries
- Start with a deterministic fake model and tools; then add a provider adapter
  supporting validated structured tool calls.
- Keep the orchestrator independent of LangChain/LangGraph and any provider SDK.
- Register typed tools explicitly. Validate model outputs and tool arguments;
  enforce step/token budgets and tool permissions before execution.
- Treat prompts and retrieved content as untrusted data. Never expose arbitrary
  shell execution, unrestricted URLs, secrets, or unrestricted filesystem tools.
- Restrict outbound destinations and redirects; bound response sizes and redact
  credentials and sensitive payloads in logs/traces.

## Verification and evidence
- Test queue saturation, actual I/O overlap, concurrency caps, job failure
  isolation, deadlines, retry classification, cancellation races, shutdown,
  history eviction, and API responsiveness under load.
- Use local mock services and deterministic failures by default. Live provider
  calls require explicit opt-in and credentials; never make them in normal CI.
- Benchmark sequential versus concurrent execution with the same workload.
  Report hardware, configuration, workload, duration, throughput, p50/p95/p99,
  queue wait, event-loop lag, memory, and failure categories.
- Do not equate 100 accepted jobs with 100 concurrently executing jobs or claim
  production readiness from a mocked benchmark.
- Run relevant tests, Ruff, and mypy for changed code. Report checks not run.

## Working conventions
- Apply SOLID pragmatically: keep one clear responsibility per module, depend
  on small behavior-focused protocols, and wire implementations at the app
  boundary. Prefer composition over inheritance. Add abstractions only for
  actual substitution points; avoid generic frameworks and pass-through layers.
- Keep imports directional and navigation obvious. Do not create empty packages
  for future milestones, circular imports, or one-file-per-trivial-class sprawl.
- Prefer descriptive names and types over comments. Comment only non-obvious
  decisions, invariants, or constraints; do not narrate what code already says.
- Prefer small typed modules and explicit configuration over framework layers.
- Keep secrets out of source control; provide .env.example with placeholders.
- Keep README and PLAN.md aligned with implemented guarantees and limitations.
- Avoid unrelated changes and new infrastructure without a clear requirement.
- Do not publish, deploy, send messages, or spend on provider calls unless asked.
- Do not spawn sub-agents unless the user explicitly requests delegation.
