# Contributing to RunWeave

Thanks for helping. RunWeave is licensed under Apache-2.0. By submitting a
contribution you agree it is licensed under the same terms (Apache-2.0,
section 5). There is no separate CLA.

## Before you start

- Read [PLAN.md](PLAN.md) for milestone scope and [AGENTS.md](AGENTS.md) for the
  architecture, async, security, and design rules. Both apply to human and AI
  contributors.
- Open an issue before large changes so scope can be agreed first.
- Report security problems privately; see [SECURITY.md](SECURITY.md).

## Development setup

Requirements: Python 3.11, 3.12, or 3.13; [uv](https://docs.astral.sh/uv/); and
Node.js 22.12+ or 24+ for the workspace UI.

```bash
uv sync --locked --extra workflow --extra eval
npm --prefix frontend ci
```

On Windows PowerShell use `npm.cmd` instead of `npm`. To test a specific Python
version, add `--python 3.11` (or 3.12/3.13) to `uv sync`.

## How we work

- **Test first.** Write a failing test that states the required behavior, make
  it pass with the smallest change, then refactor. Bug fixes start with a test
  that reproduces the bug.
- **Deterministic and offline.** Tests use mock transports and fixtures. Never
  call live model providers or external services from the default test suite.
- **Small, focused changes.** One concern per pull request. Avoid unrelated
  refactors and new infrastructure without a clear requirement.
- **Design.** Keep one clear responsibility per module, depend on small
  protocols, wire implementations at the application boundary, and prefer
  composition. Add abstractions only for real substitution points.
- **Security.** Treat prompts, model output, tool output, and retrieved content
  as untrusted. Never commit secrets; use `.env` (ignored) and keep
  `.env.example` placeholders only. Keep outbound destinations restricted and
  redact credentials from logs, traces, and errors.
- **Compatibility.** Code must run on Python 3.11. Avoid newer syntax such as
  PEP 695 type parameters.

## Checks before opening a pull request

```bash
uv run --no-sync pytest
uv run --no-sync ruff check .
uv run --no-sync ruff format --check .
uv run --no-sync mypy
npm --prefix frontend test
```

PostgreSQL and native sandbox tests are opt-in; see the README for
`scripts/verify-postgres.ps1` and `scripts/verify-isolation.ps1`. CI runs the
suite on Python 3.11–3.13 on Linux and Windows and audits dependencies.

## Pull requests

- Describe what changed, why, and how you tested it. Note any checks you could
  not run.
- Update README, PLAN.md, docs, and CHANGELOG.md when behavior, guarantees, or
  limitations change.
- Dependency changes must keep `uv.lock` and `frontend/package-lock.json`
  committed and pass the dependency audit.
