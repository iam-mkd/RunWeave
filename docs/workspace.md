# Local React workspace

The workspace is a React UI for the RunWeave APIs, served by `runweave serve`. It works
with every registered agent, including your own Python agents (see the
[agent guide](agents.md)):

| Screen | What it does |
| --- | --- |
| Agents (`#/agents`) | Registered agents, their connection, version and recent success rate |
| Settings (`#/settings`) | Proxy mode and allowed models, provider keys (stored in the OS credential store, never shown), budgets per run, fault profile and seed, and the environment lines that keep them |
| Connect (`#/setup`) | Python: generates the `runweave serve --agent …` command and detects the agent after restart. HTTP: tests a loopback URL, registers its agents until the server stops, and shows the `--http-agent` startup command. Both link to a smoke test |
| Runs (`#/runs`) | Your runs, newest first, filtered by agent and state; open any run by ID |
| New run (`#/runs/new`) | A form generated from the agent's input JSON Schema (raw JSON for schemas it cannot represent) |
| Run detail (`#/runs/<id>`) | Live status, step timeline, output, input, usage, cancel and re-run; approvals for durable triage runs |
| Load tests (`#/load-tests`) | Runs `runweave bench` for the project: concurrency levels, fault profiles and a soak, with live charts and saved reports |
| Evals (`#/evals`) | Runs `runweave eval` on a dataset: cases, assertions, gates and saved reports |
| Shadow (`#/shadow`) | Runs `runweave shadow`: a candidate beside the current agent, the promotion gates and paired cases |
| Approvals (`#/approvals`) | Requests from agents waiting for a person (durable mode): exact arguments, confirmed approve and reject |

Run detail follows the run over server-sent events and falls back to polling once a
second when streams are busy or unavailable. Old `/?run=<id>` links still open the run.

## A tour of the workspace

Start it from your project folder with your agent loaded, then open
<http://127.0.0.1:8000>:

```bash
runweave serve --agent agent:run
```

1. **Agents** is the home screen: every registered agent with how it connects (Python or
   HTTP), its version, and how many of its recent runs succeeded. **Connect agent** walks you
   through adding a Python or HTTP agent.
2. **New run** on an agent's row opens a form built from its input schema. **Start run**
   submits it and opens the run.
3. **Run detail** follows the run live: its status, a timeline of steps (model calls with
   their token usage, tool calls, the steps you named, injected faults), then the output,
   input and usage. **Cancel run** stops a running run; **Re-run** opens the form with the same input.
4. **Runs** lists your runs, newest first. Filter by agent and state, or paste a run ID.
5. **Evals** runs `runweave eval` on one of the project's datasets and shows each case with
   its assertions, the gates, and every saved eval report.
6. **Load tests** runs `runweave bench`: pick concurrency levels, runs per level, fault
   profiles and a soak, and watch throughput, latency and failures chart live.
7. **Shadow** compares a candidate version with the current agent on a dataset and shows
   the promotion gates and each paired case.
8. **Approvals** (durable mode) lists requests from agents waiting for a person, with the
   exact arguments, and approves or rejects them after you confirm.
9. **Settings** switches the proxy between mock and your own key, stores provider keys in
   your OS credential store, sets per-run budgets, and picks a fault profile and seed.

The theme switch chooses dark, light, or your system's setting. The screenshots below show every screen.

## Screens

Every screen in both themes, from a seeded project (a real eval with one failing case, a
load test with two fault profiles and a short soak, a shadow comparison against a changed
version, and approval requests from the sample refund agent). The tables and charts are
real output from this machine, not fixtures.

| Screen | Dark | Light |
| --- | --- | --- |
| Agents | ![Agents, dark theme](images/agents-dark.png) | ![Agents, light theme](images/agents-light.png) |
| Runs | ![Runs, dark theme](images/runs-dark.png) | ![Runs, light theme](images/runs-light.png) |
| New run | ![New run, dark theme](images/new-run-dark.png) | ![New run, light theme](images/new-run-light.png) |
| Run detail | ![Run detail, dark theme](images/run-detail-dark.png) | ![Run detail, light theme](images/run-detail-light.png) |
| Load tests | ![Load tests, dark theme](images/load-tests-dark.png) | ![Load tests, light theme](images/load-tests-light.png) |
| Evals | ![Evals, dark theme](images/evals-dark.png) | ![Evals, light theme](images/evals-light.png) |
| Shadow | ![Shadow, dark theme](images/shadow-dark.png) | ![Shadow, light theme](images/shadow-light.png) |
| Approvals | ![Approvals, dark theme](images/approvals-dark.png) | ![Approvals, light theme](images/approvals-light.png) |
| Settings | ![Settings, dark theme](images/settings-dark.png) | ![Settings, light theme](images/settings-light.png) |
| Connect | ![Connect an agent, dark theme](images/setup-dark.png) | ![Connect an agent, light theme](images/setup-light.png) |

To regenerate them after a UI change, build the workspace and run the script. With
`RUNWEAVE_SMOKE_POSTGRES_URL` set to a PostgreSQL server it also captures Approvals; it
creates and drops its own database.

```powershell
npm.cmd --prefix frontend run build
npm.cmd --prefix frontend run screenshots
```

The README's 12-second demo (`docs/images/demo.gif`) is recorded the same way, from a live
`runweave serve` with `examples/plain-python`. The terminal scene replays real `runweave
eval` and `runweave bench` output; only the captions and pointer are drawn on top:

```bash
npm --prefix frontend run demo-gif
uv run --no-project --with pillow python scripts/frames-to-gif.py target/demo-gif/frames docs/images/demo.gif
```

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
text, admission failures, expired IDs, setup detection, connecting, running and
removing a real HTTP agent (the Python contract fixture) and both themes.
The browser approval-binding scenario uses an intercepted durable API fixture;
the existing PostgreSQL suite separately verifies actual approval persistence
and effects. Screenshots are written to ignored `frontend/test-results/`.
