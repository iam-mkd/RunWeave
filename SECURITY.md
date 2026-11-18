# Security policy

## Supported versions

RunWeave has not published a release yet. Security fixes land on `main` and in
the next release. After 1.0, the latest minor release receives fixes.

## Reporting a vulnerability

Please report vulnerabilities privately through GitHub's **Report a
vulnerability** button on the repository's **Security** tab (private
vulnerability reporting). Do not open a public issue for a suspected
vulnerability.

Include the affected version or commit, configuration, reproduction steps, and
impact. We aim to acknowledge reports within seven days and to agree on a
disclosure date once a fix is available.

## Scope

RunWeave is verified for local development only. Before exposing it publicly,
apply the deployment gate in [docs/architecture.md](docs/architecture.md). The
threat model and trust boundaries are in the same document, with details in
[docs/security.md](docs/security.md) and [docs/isolation.md](docs/isolation.md).

Out of scope: issues that require an already-trusted operator, database
administrator, or Docker host; findings that rely on running RunWeave in a
configuration the documentation marks as development-only (for example `local`
auth mode exposed to a network); and live-model behavior that the offline
evaluation fixtures do not claim to cover.

## Dependency auditing

CI audits Python dependencies (`pip-audit` against `uv.lock`) and frontend
dependencies (`npm audit`). Shipped frontend dependencies must have no known
advisories. Development-only tooling advisories are listed below when no
patched version exists, with the reason they are accepted.

| Advisory | Package | Where | Status |
| --- | --- | --- | --- |
| [GHSA-fc5q-39xc-vjpf](https://github.com/advisories/GHSA-fc5q-39xc-vjpf) (ReDoS) | `picomatch` ≤ 4.0.7, via `vite` | Frontend build tooling only; not in the built UI | Accepted until a patched release exists. Vite matches only project-controlled glob patterns, never user input. Recheck when Dependabot reports a fix. |
