# Changelog

All notable changes to RunWeave are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-11-20

The first public release. Install it with `uv tool install runweave`, `pipx install
runweave` or `pip install runweave`; the TypeScript SDK is `runweave-agent` on npm. It
covers milestones 1–27. Milestones 1–16 and the first workspace brought bounded async
scheduling, deadlines, retries, cancellation and graceful shutdown; incident-triage
workflows (baseline, PydanticAI, coordinated); observability and load benchmarks;
PostgreSQL durable recovery, checkpoints, approvals, scoped memory and budgets;
authentication and prompt-injection defenses; sandboxed Python and browser tools; and
offline evaluation and shadow comparison. Everything after that is listed below. See
PLAN.md for the verified scope and known limits.

### Added

- First public release (milestone 27):
  - **PyPI:** the release workflow publishes the wheel and sdist to PyPI by trusted
    publishing (no token stored), from the same checksummed files as every other channel,
    with attestations, after the owner approves the `release` environment.
  - **Install check:** every pull request installs the built wheel with `pip` and with
    `uv tool install` on fresh Windows, macOS and Linux runners, with no Node and no
    clone, and runs `init`, `eval`, `bench`, `shadow` and the workspace from it.
  - **`runweave --version`** prints the installed version.
  - **Quickstart:** installs the package instead of cloning; building from source moves
    to its own section. Docs CI runs it on three systems with the wheel the pull request
    built, substituting only the install line.
  - **Package metadata:** PyPI description, keywords, classifiers and project links, and
    a README whose links and images are absolute so they work on pypi.org; npm keywords,
    repository (required for provenance), homepage and bugs.

- Developer documentation (milestone 26, part 1):
  - **Index and quickstart:** `docs/README.md` indexes every page. `docs/quickstart.md`
    takes a new developer from a clone to an evaluated, load-tested agent running in the
    workspace, in about five minutes and with no API key.
  - **New pages:** concepts, reading reports, the fault catalog, limits (a mock
    benchmark is not provider capacity) and troubleshooting. The workspace guide gains a
    tour, and the budgets guide gains a bring-your-own-key section.
  - **References:** the CLI and configuration references (every `runweave.toml` key and
    `RUNWEAVE_*` setting) and the fault tables are generated from the code by
    `scripts/reference-docs.py`, and CI fails when they drift.
  - **Docs CI:** `scripts/check-docs.py` checks every link and anchor. It also runs the
    quickstart's commands as written on fresh Windows, macOS and Linux runners, keeping
    each transcript (`.github/workflows/docs.yml`).
- Framework examples (milestone 26, part 2): `examples/plain-python` (the `openai` client
  and a Chat Completions tool loop), `examples/openai-agents` (OpenAI Agents SDK on the
  Responses API, tracing off), `examples/claude-agent-sdk` (the SDK's bundled Claude Code
  CLI on the Anthropic Messages endpoint with an in-process MCP tool), `examples/langgraph`
  (`create_react_agent` with `langchain-openai`) and `examples/typescript` (`@runweave/agent`
  with the `openai` npm client). Each is a project with exactly pinned dependencies, a
  dataset and mock fixtures. `.github/workflows/examples.yml` runs `runweave eval` and
  `runweave bench` for each one with no provider key and all outbound traffic blocked.
  See `docs/examples.md`. `examples/typescript` replaces `examples/http-agent-typescript`.
- Desktop releases (milestone 25, part 4; completes milestone 25):
  - **Draft releases:** for the owner's tag, the draft release attaches the unsigned
    installers (MSI and NSIS, DMG, `.deb`, `.rpm`, AppImage), `THIRD_PARTY_NOTICES.txt`
    and a CycloneDX SBOM of the window's Rust crates, all covered by `SHA256SUMS`.
  - **Clean-runner check:** `scripts/check-desktop.py` installs each installer on a fresh
    Windows, macOS or Linux runner. It runs an eval, a load test, a run and an approved
    agent action through the app, quits, uninstalls, and fails if any process is left.
  - **License notices:** every installer carries `THIRD_PARTY_NOTICES.txt`, with the
    license texts of everything bundled.
  - **Docs:** `docs/desktop.md` covers install, first launch of an unsigned app, uninstall
    and data removal.
- Desktop window (milestone 25, part 3): a Tauri 2 app (`desktop/`).
  - **First launch:** a setup page to choose the project folder and Python interpreter
    (saved for later launches). It starts the desktop service and shows its workspace,
    or explains why the service could not start.
  - **Quitting** stops the service gracefully.
  - **Access:** only the bundled setup page can call the app's four commands, and the
    window navigates only to it and the service's loopback origin.
  - **CI** builds unsigned installers on Windows, macOS and Linux, after the Rust tests,
    rustfmt and clippy.
- Desktop service (milestone 25, part 2): `runweave-desktop-service`, the program the
  desktop app runs. It:
  - unpacks pinned, checksum-verified PostgreSQL 18.7 into app-data;
  - runs PostgreSQL on loopback with a generated password, and applies the migrations;
  - runs the project's Python agents with the developer's own interpreter, through
    `serve-agent` (eval, bench and shadow jobs use it too, via the new
    `RUNWEAVE_JOB_PYTHON`);
  - serves the workspace, and stops everything when its stdin closes.

  `scripts/build-sidecar.py` freezes it with PyInstaller and smoke-tests the build; a
  new CI workflow does both on Windows, Linux and macOS. See `docs/desktop.md`.
- Service image, Compose and releases (milestone 25, part 1):
  - **Image:** the service image now serves the workspace UI and supports durable mode
    (the new `durable` extra, migrations and the workflow extra) through
    `runweave serve --container`.
  - **Compose:** `compose.yaml` runs PostgreSQL 18, a one-shot migration and RunWeave on
    `127.0.0.1` only. The password goes in `compose.env`.
  - **Release workflow:** `.github/workflows/release.yml` builds and checks on every pull
    request: the wheel and sdist, the SDK tarball, CycloneDX SBOMs, `SHA256SUMS`, and the
    image through Compose. It publishes to npm (with provenance) and GHCR, and creates a
    draft GitHub Release, only for a version tag the owner pushes or a manual run, behind
    the `release` environment.
  - **CI:** CI runs actionlint.
- UI polish and screenshots (milestone 24, part 3; completes milestone 24): screens
  matched to the approved mockups. Runs: "Recent runs", a run count, 24-hour times and
  sortable columns. Evals: the run form in the page header and run states as badges.
  Shadow: each gate shows the candidate's measured value, and the form is aligned. Load
  tests: summary tiles for an open report. Connect: current LLM proxy copy. The demo
  triage summary no longer doubles a full stop. `npm run screenshots` captures every
  screen in both themes to `docs/images`, shown in README and `docs/workspace.md`.
- Large data (milestone 24, part 2): tables show 100 rows at a time with an accessible
  pager; eval reports start on failed cases and can be searched, shadow reports start on
  changed pairs; long live charts are downsampled. `npm run test:perf` checks budgets with
  thousands of runs and 5,000-case reports, in CI.
- Accessibility audit (milestone 24, part 1): the browser smoke test audits every screen
  it reaches with axe-core (WCAG 2.1 AA) in both themes at desktop and 390 px, and checks
  keyboard paths, focus management and reduced motion. Fixes:
  - scrolling tables and code blocks are keyboard-reachable while they overflow;
  - form errors move focus to the first invalid field;
  - the approval confirmation takes focus, Escape cancels it, and focus stays on the panel
    after a decision.
  Setup reuses the shared command block. New development dependency: axe-core (MPL-2.0).
- Approvals screen and `runweave approvals` (milestone 23, part 3): a pending inbox with the
  exact arguments, digest, policy and expiry, confirmed Approve and Reject, and a link from
  waiting runs; `runweave approvals list|show|approve|reject` with confirmation and exit
  codes. The sample agent gains `refund_request`. The workspace CI job now runs the browser
  approval checks on PostgreSQL.
- Agent approvals for HTTP agents (milestone 23, part 2): `ctx.requestApproval` in the
  TypeScript SDK and `ctx.request_approval` for agents served with `runweave.sdk_http`.
  Durable run requests carry an `approvals_url`; agents call `POST /agent/v1/approvals`
  with their run token, repeating a bounded wait until decided, with their own deadline
  paused. Unreachable callbacks are retried until RunWeave ends the run. The shared contract
  suite covers both SDKs on PostgreSQL.
- Agent approvals (milestone 23, part 1): `ctx.request_approval(action, arguments,
  expires_in)` for Python agents in durable mode. Requests are bound to tenant, run,
  action, argument digest, policy version and expiry; decisions are idempotent and
  opposing ones conflict; a re-run repeating the same request gets the existing request or
  decision. The waiting agent keeps its worker slot (at most half the pool can wait) and
  its run deadline is paused. New `/v1/approvals` API and migration
  `0008_action_approvals`. See `docs/agent-approvals.md`.
- Workspace design system (milestone 18): dark-first tokens with a light theme
  and a System / Dark / Light switch, WCAG 2.1 AA contrast tests, and shared
  components (app shell, tables, status badges, stat tiles, timeline, charts,
  empty/error/loading states). See `docs/design-system.md`.
- Clickable design prototype of the developer-platform screens with synthetic
  data (`npm --prefix frontend run dev:prototype`), checked in CI.
- Frontend unit tests now run on Vitest.
- Python agent SDK (`runweave.sdk`): `@agent` with Pydantic input/output models and a
  `RunContext` (deadline, attempt, step timeline, redacted logging, usage). Agents load
  at startup with `runweave serve --agent module:attribute` or `RUNWEAVE_AGENTS`; see
  `docs/agents.md` and `examples/sample-agent`.
- `runweave` command (`serve`, with `--check`); `runweave-workspace` remains an alias.
- `GET /v1/agents`, `GET /v1/runs` (owner-scoped, filtered, bounded) and
  `GET /v1/runs/{id}/events` (bounded server-sent events).
- Run snapshots include `agent`, `agent_version` and `steps`.
- Migration `0007_generic_agents` (agent identity, step timeline, owner index).
- Workspace screens for any agent: Agents, Connect (startup-only registration with
  detection and smoke test), Runs (server-side history with filters), New run (form
  generated from the agent's input schema) and Run detail (live server-sent updates
  with polling fallback, timeline, output, input, cancel, re-run, approvals).
- Run snapshots include the validated `input`; run listings do not.
- HTTP agents (milestone 20): the versioned `runweave.agent/v1` contract
  (`contracts/agent-http-v1.openapi.json`), an adapter with loopback-only destinations,
  no redirects, bounded responses, classified retries with an idempotency key, and
  best-effort cancel calls. Register at startup (`runweave serve --http-agent URL`,
  `RUNWEAVE_HTTP_AGENTS`) or at runtime (`POST /v1/agents/http`, `/check`,
  `DELETE /v1/agents/{name}`, new `agents:manage` scope). `runweave serve-agent` and
  `runweave.sdk_http` serve Python agents over the contract. See `docs/http-agents.md`.
- New dependency: `jsonschema` (MIT), for HTTP agents' JSON Schemas.
- TypeScript agent SDK `@runweave/agent` (`sdk/typescript`, unpublished, no runtime
  dependencies): `defineAgent`, `serveAgents`, `createAgentHandler`, with the same
  context, limits, idempotency and cancellation behavior as the Python wrapper, plus
  `examples/http-agent-typescript`. The shared contract suite now runs against both SDKs.
- LLM proxy, mock mode (milestone 21): OpenAI Chat Completions and Responses at
  `/llm/v1` and Anthropic Messages at `/llm/v1/messages`, with streaming and tool calls.
  Per-run proxy tokens (`ctx.proxy`, also sent to HTTP agents in the run request), budget
  reservation before answering and settlement after, "Model call" timeline steps
  without prompt text, and fixture-scripted deterministic replies
  (`RUNWEAVE_PROXY_MOCK_FIXTURES`). Verified with the official OpenAI and Anthropic SDKs
  for Python and Node and with PydanticAI. See `docs/llm-proxy.md`.
- Release readiness (milestone 22): wheels carry the built workspace UI
  (`scripts/bundle-workspace-ui.py`), so `uvx runweave serve` needs no Node;
  `scripts/check-release.py` checks a clean `uvx` install end to end (CLI and UI, Python
  and TypeScript projects) and runs in CI on Linux and Windows; a copy-ready GitHub Actions
  job (`examples/github-actions`) fails pull requests that regress the agent;
  `scripts/measure-ui-overhead.py` shows load tests are not slowed by an open screen.
  `runweave serve --agent module:fn` now imports from the working folder when run as the
  installed command.
- Load tests, Evals and Shadow screens (milestone 22): the workspace starts
  `runweave bench|eval|shadow --progress` as a child process through a new jobs API
  (`/v1/jobs`, `/v1/reports`), charts load tests live, and lists saved reports from the
  CLI and UI alike. One job at a time; cancelling stops the command cleanly. `runweave
  serve` picks up `./runweave.toml` or `--project`. New scopes `tests:run` and
  `tests:read`.
- `runweave bench` (milestone 22): concurrency sweeps, fault profiles and soak runs
  against the project's agent, reporting hardware, settings, throughput, p50/p95/p99
  latency and queue wait, status-API latency, event-loop lag, memory, failure categories,
  tokens and cost, with optional failure-rate and p95 gates. `runweave shadow` runs a
  candidate version beside the current one, records paired differences and applies the
  milestone 16 promotion gates. In-process commands now serve RunWeave on a loopback port
  so agents reach the LLM proxy. psutil becomes a runtime dependency (BSD-3-Clause).
- Testing CLI (milestone 22): `runweave init` creates a Python or TypeScript project
  (with the TypeScript SDK vendored) and `runweave.toml`; `runweave eval` runs JSONL or
  YAML cases with built-in assertions, custom Python checks and Pydantic Evals, writes
  JSON, JUnit XML and Markdown reports, gates on pass rate and baseline regressions, and
  runs in-process or against `--server`. `runweave serve --api-only`. New dependency:
  PyYAML (MIT). See `docs/testing.md`.
- LLM proxy fault injection: seeded profiles (built-in `flaky`, `rate-limited`, `outage`,
  `slow`, `broken-tools`, `chaos`, plus your own) with 429s, 5xx, timeouts, slow first
  tokens, mid-stream disconnects, malformed tool-call JSON, unknown tool names and
  context-length errors, in each provider's format. The same seed reproduces the same
  faults.
- LLM proxy passthrough: forwards to OpenAI- and Anthropic-compatible providers with keys
  RunWeave holds, a model allowlist, output limits capped to the budget reservation,
  usage settled from responses and streams, relayed provider errors, and per-provider
  concurrency and rate limits.
- Settings screen and API: proxy mode and models, provider keys in the OS credential store
  (keyring; write-only, never returned, logged or shown), budgets per run, and the fault
  profile and seed, changed while the server runs and validated all or nothing. New
  `settings:manage` scope and `keyring` dependency (MIT).
- Run detail marks proxied model calls and injected faults; steps carry `kind` and
  `fault`.
- Workspace: connect HTTP agents from Connect (test the URL, see conflicts, register,
  copy the startup command) and remove runtime-registered agents on the Agents screen.

- Apache-2.0 LICENSE and NOTICE, CONTRIBUTING, SECURITY, and this changelog.
- Dependency license audit in `docs/licenses.md`.
- CI workflow running tests, Ruff, and strict mypy on Python 3.11, 3.12, and
  3.13 on Linux and Windows, plus Python and frontend dependency audits.
- `scripts/verify-postgres.ps1` and `scripts/verify-isolation.ps1`, which the
  documentation already referenced.

### Changed

- The TypeScript SDK is now `runweave-agent` on npm, not `@runweave/agent`: the `@runweave`
  npm scope belongs to an unrelated project. A TypeScript project created by an earlier
  `runweave init` imports `'@runweave/agent'`; change the import and the dependency's name
  in `package.json` to `runweave-agent`.

- `runweave serve` from a source checkout finds the checkout's built UI from any folder,
  not only from the checkout's root (milestone 26).
- The incident-triage workspace uses the new navigation shell and components;
  behavior is unchanged.
- The workspace no longer keeps recent run IDs in browser storage; history comes from
  the server. Screens use hash links (`#/runs/<id>`); old `?run=` links still work.
- `POST /v1/runs` accepts `{"agent", "input"}`; the incident body still runs the
  built-in `incident-triage` agent. Snapshot `result` is now plain JSON for every agent.
- Python support widened from 3.13 only to 3.11 and newer. Ruff and mypy now
  target 3.11.
- Package metadata declares the license and supported Python versions.
- Offline mode still routes model and tool traffic to the local mocks; only loopback
  hosts (and hosts in `RUNWEAVE_HTTP_AGENT_ALLOWED_HOSTS`) reach the network, for HTTP
  agents.
- `RunContext` can record steps that ran elsewhere (`add_finished_steps`).
- `runweave bench` costs less CPU per run. It reads each run's event stream to the end so
  the connection is reused, keeps a connection per run in flight with `--server`, and
  gives each block of 8 run slots its own HTTP client. Admission now counts a tenant's
  unfinished runs without scanning the history. On one 16-CPU Windows machine, an agent
  with a 50 ms wait went from about 51 to 187 runs/s in-process at concurrency 64, and
  from 127 to 305 runs/s with `--server`; see `benchmarks/README.md`.

### Security

- `POST /v1/runs` bodies are size-limited before parsing (`RUNWEAVE_RUN_REQUEST_MAX_BYTES`).
- OpenTelemetry raised to 1.45.2 (fixes CVE-2026-114603 and CVE-2026-126360).
- Frontend `vite` raised to 7.3.8 and `esbuild` to 0.28.2 (fixes
  GHSA-g7r4-m6w7-qqqr, development-server file read on Windows).

[Unreleased]: https://github.com/iam-mkd/RunWeave/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/iam-mkd/RunWeave/releases/tag/v0.1.0
