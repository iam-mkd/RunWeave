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

- Apache-2.0 LICENSE and NOTICE, CONTRIBUTING, SECURITY, and this changelog.
- Dependency license audit in `docs/licenses.md`.
- CI workflow running tests, Ruff, and strict mypy on Python 3.11, 3.12, and
  3.13 on Linux and Windows, plus Python and frontend dependency audits.
- `scripts/verify-postgres.ps1` and `scripts/verify-isolation.ps1`, which the
  documentation already referenced.

### Changed

- The incident-triage workspace uses the new navigation shell and components;
  behavior is unchanged.
- Python support widened from 3.13 only to 3.11 and newer. Ruff and mypy now
  target 3.11.
- Package metadata declares the license and supported Python versions.

### Security

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
