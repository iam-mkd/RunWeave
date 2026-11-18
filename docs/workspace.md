# Local React workspace

The workspace is a React UI for the RunWeave APIs, served by `runweave serve`. It works
with every registered agent, including your own Python agents (see the
[agent guide](agents.md)):

| Screen | What it does |
| --- | --- |
| Agents (`#/agents`) | Registered agents, their connection, version and recent success rate |
| Connect (`#/setup`) | Generates the `runweave serve --agent …` command, detects the agent after restart, links to a smoke test |
| Runs (`#/runs`) | Your runs, newest first, filtered by agent and state; open any run by ID |
| New run (`#/runs/new`) | A form generated from the agent's input JSON Schema (raw JSON for schemas it cannot represent) |
| Run detail (`#/runs/<id>`) | Live status, step timeline, output, input, usage, cancel and re-run; approvals for durable triage runs |

Run detail follows the run over server-sent events and falls back to polling once a
second when streams are busy or unavailable. Old `/?run=<id>` links still open the run.

## Start from a checkout

Use Python 3.11 or newer, uv, and Node 22.12+ or 24+. From the repository root, in PowerShell:

```powershell
uv sync --locked
npm.cmd --prefix frontend ci
npm.cmd --prefix frontend run build
uv run --locked runweave-workspace --demo
```

On macOS/Linux, use `npm` instead of `npm.cmd`. Open **http://127.0.0.1:8000**.
No database or provider credentials are required with the default configuration.
Choose **New run** for `incident-triage`, then **Load example** and **Start run** to try
the offline demo. Add your own agent with `--agent mypkg.agent:run`
(`runweave-workspace` is an alias of `runweave serve`).

The `--demo` launcher ignores `.env`, forces offline inference and in-memory runs,
and disables isolated tools. Omit `--demo` to use your configured backend settings;
use `--port 8001` if port 8000 is occupied. No keys or subscription tokens are requested or
stored in the browser. The provider badge reflects the backend configuration;
account sign-in and connection setup are separate future work.

Runs in the default in-memory mode are lost when the server stops. Run history comes
from the server (`GET /v1/runs`, scoped to the caller); the browser stores only the theme
choice. A stale ID displays an unavailable/expired message. Every screen has its own
link, so refreshing keeps your place while the backend still retains the run.

If an existing durable deployment is configured, the UI exposes scoped recall
and sandbox ticket proposals, and shows the exact pending proposal for approval
or rejection. It sends the server's approval bindings unchanged. Those controls
are hidden in in-memory mode. Database setup and local distribution choices are
unchanged by this frontend. Follow the existing [durable/approval guide](approvals.md)
and use `uv run --no-sync runweave-workspace` after installing the durable
dependencies and workflow extra, so uv does not remove those additional packages.

## Local development

Keep the backend running with `runweave-workspace` and run this in another terminal:

```powershell
npm.cmd --prefix frontend run dev
```

Open the Vite URL printed in that terminal. The dev server proxies `/v1` and
`/health` to `127.0.0.1:8000`. A production build is served by FastAPI itself,
with relative same-origin API requests and no permissive CORS configuration.
Rebuild the frontend to update the page served on port 8000.

The UI follows the [workspace design system](design-system.md): dark-first with
a light theme (System / Dark / Light switch in the sidebar), shared components in
`frontend/src/components/`, and tokens in `frontend/src/design/tokens.css`. To
review the planned developer-platform screens with synthetic data, run
`npm.cmd --prefix frontend run dev:prototype`. The prototype is built separately
and never ships in the workspace bundle.

The workspace launcher binds only to `127.0.0.1`, requires local auth mode,
restricts the Host header, and rejects cross-origin browser API requests while
allowing normal link navigation to the workspace page. Its content
security policy permits only local scripts/styles/connections. It does not read
or copy `.env` into the browser bundle. This is a local development UI, not an
authenticated multi-user web deployment; retain the [public deployment gate](architecture.md).

## Verification

```powershell
npm.cmd --prefix frontend test
npm.cmd --prefix frontend run build
uv run --no-sync pytest tests/test_workspace.py -q
npm.cmd --prefix frontend run test:browser
npm.cmd --prefix frontend run build:prototype
npm.cmd --prefix frontend run test:prototype
```

Unit tests (Vitest) cover token contrast in both themes (WCAG 2.1 AA), theme
preference handling, formatting, navigation, and component structure and
escaping. The prototype check serves the built prototype under the workspace's
content security policy and visits every screen in both themes at desktop and
phone widths.

The browser check starts and stops its own fake-mode FastAPI server on an
ephemeral loopback port. It uses installed Microsoft Edge on Windows; elsewhere
install Playwright Chromium first (`npx playwright install chromium` from
`frontend`). `PLAYWRIGHT_CHANNEL` can select another installed Playwright browser
channel. No live model calls or existing databases are used.

Checks cover the Agents, Connect, Runs, New run and Run detail screens against a real
offline server with the sample agent loaded: generated forms, server-side validation
errors on fields, live updates, triage findings, the SDK agent's timeline and re-run,
cancellation, reload, old `?run=` links, mobile overflow, safe rendering of untrusted
text, admission failures, expired IDs, setup detection and both themes.
The browser approval-binding scenario uses an intercepted durable API fixture;
the existing PostgreSQL suite separately verifies actual approval persistence
and effects. Screenshots are written to ignored `frontend/test-results/`.
