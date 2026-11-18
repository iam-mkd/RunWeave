# Local React workspace

The workspace connects a React UI to the existing FastAPI APIs. It supports
incident submission, status polling, cancellation, findings/evidence, token and
estimated-cost reporting, recent run IDs and direct run links. It uses the
existing incident-triage workflow; it is not a general-purpose coding/chat agent.

## Start from a checkout

Use Python 3.13, uv, and Node 22.12+ or 24+. From the repository root, in PowerShell:

```powershell
uv sync --locked
npm.cmd --prefix frontend ci
npm.cmd --prefix frontend run build
uv run --locked runweave-workspace --demo
```

On macOS/Linux, use `npm` instead of `npm.cmd`. Open **http://127.0.0.1:8000**.
No database or provider credentials are required with the default configuration.
Use **Load example**, then **Start run** to try the offline demo.

The `--demo` launcher ignores `.env`, forces offline inference and in-memory runs,
and disables isolated tools. Omit `--demo` to use your configured backend settings;
use `--port 8001` if port 8000 is occupied. No keys or subscription tokens are requested or
stored in the browser. The provider badge reflects the backend configuration;
account sign-in and connection setup are separate future work.

Runs in the default in-memory mode are lost when the server stops. The browser
retains at most twelve run IDs, not descriptions, findings or credentials. A
stale ID displays an unavailable/expired message. Refreshing the page restores
the selected run using its `?run=<UUID>` link while the backend still retains it.

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
```

The browser check starts and stops its own fake-mode FastAPI server on an
ephemeral loopback port. It uses installed Microsoft Edge on Windows; elsewhere
install Playwright Chromium first (`npx playwright install chromium` from
`frontend`). `PLAYWRIGHT_CHANNEL` can select another installed Playwright browser
channel. No live model calls or existing databases are used.

Checks cover real API submission/results/cancellation, reload/history, mobile
overflow, safe rendering of untrusted text, admission failures and expired IDs.
The browser approval-binding scenario uses an intercepted durable API fixture;
the existing PostgreSQL suite separately verifies actual approval persistence
and effects. Screenshots are written to ignored `frontend/test-results/`.
