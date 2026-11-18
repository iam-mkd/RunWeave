# Changelog

All notable changes to RunWeave are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

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

- Apache-2.0 LICENSE and NOTICE, CONTRIBUTING, SECURITY, and this changelog.
- Dependency license audit in `docs/licenses.md`.
- CI workflow running tests, Ruff, and strict mypy on Python 3.11, 3.12, and
  3.13 on Linux and Windows, plus Python and frontend dependency audits.
- `scripts/verify-postgres.ps1` and `scripts/verify-isolation.ps1`, which the
  documentation already referenced.

### Changed

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

### Security

- `POST /v1/runs` bodies are size-limited before parsing (`RUNWEAVE_RUN_REQUEST_MAX_BYTES`).
- OpenTelemetry raised to 1.45.2 (fixes CVE-2026-114603 and CVE-2026-126360).
- Frontend `vite` raised to 7.3.8 and `esbuild` to 0.28.2 (fixes
  GHSA-g7r4-m6w7-qqqr, development-server file read on Windows).

## 0.1.0 — 2026-11-18 (not published)

Milestones 1–16 and the local React workspace: bounded async scheduling,
deadlines, retries, cancellation and graceful shutdown; incident-triage
workflows (baseline, PydanticAI, coordinated); observability and load
benchmarks; PostgreSQL durable recovery, checkpoints, approvals, scoped memory,
and budgets; authentication and prompt-injection defenses; sandboxed Python and
browser tools; offline evaluation and shadow comparison. See PLAN.md for the
verified scope and known limits.
