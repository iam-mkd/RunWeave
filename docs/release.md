# Release verification and shadow replay

Milestone 16 provides an offline release procedure and a reproducible durable
demonstration. It does not expose a public service, switch production traffic,
add a frontend, or enable live inference. Read [architecture and operations](architecture.md)
for the state model, trust boundaries and deployment procedure.

## Clean-checkout reproduction (PowerShell 7)

Prerequisites: Python 3.13, uv, PostgreSQL 18 command-line tools, and Docker with
the Linux engine running. The PostgreSQL helper accepts `-PostgresBin` for another
installation. Only the Docker builds download dependencies. Tests and demos use
synthetic data and fake models. Run commands sequentially so an environment sync
does not remove optional database packages while another check is using them.

```powershell
uv sync --locked --extra workflow --extra eval
uv run --no-sync runweave-demo
uv run --no-sync ruff check .
uv run --no-sync ruff format --check .
uv run --no-sync mypy
uv lock --check --offline
uv run --no-sync pytest -q
```

Ordinary tests skip PostgreSQL and native worker cases unless explicitly
configured. Reproduce those independently:

```powershell
pwsh -NoProfile -ExecutionPolicy Bypass -File scripts/verify-postgres.ps1 -ReleaseReport target/evals/release-demo.json
pwsh -NoProfile -ExecutionPolicy Bypass -File scripts/verify-isolation.ps1
uv run --no-sync python -m benchmarks.socket_smoke --output target/evals/native-tcp.json
docker build -t runweave-release:m16 .
uv run --no-sync python scripts/check-container.py --image runweave-release:m16 --output target/evals/container-smoke.json
```

The process-only PowerShell execution policy permits the checked-out scripts;
it does not change the machine policy. Helpers stop/remove their disposable
database or containers in `finally` blocks and restore test environment variables.
The service image serves the workspace and the API with the fake model by default, and
includes durable mode (the `durable` and `workflow` extras and the migrations); see
[Service image and Compose](#service-image-and-compose). Durable tests run in the host
environment with `requirements-durable.txt` (the same pins as the `durable` extra) and the
workflow extra.

The release demo is `tests/test_postgres_release.py`. It creates scoped historical
evidence, submits a coordinated run, terminates a worker process with exit 17
after a committed health branch, reclaims its expired lease, and resumes it.
It verifies actual model recall of the private fact, preserves the completed
branch, waits for explicit approval, creates one local sandbox ticket, and repeats
the approval without duplicating the ticket. A side-by-side replay uses the same
incident, fixed health/runbook inputs and captured private fact. Its effects are
simulated; ticket and memory row counts remain unchanged. The report includes
trace IDs/span names and the durable budget, which survives recovery and approval.
These traces are separate attempt traces; a process crash does not preserve one
continuous trace context.
The durable demo explicitly opts into coordinated execution to demonstrate typed
delegation. The separate promotion comparison keeps baseline as the release
primary and evaluates coordinated as its candidate; the demo does not promote it.

## Shadow policy and promotion

```powershell
uv run --no-sync python -m runweave.evaluation.shadow --output target/evals/shadow.json --selection target/evals/selection.json
```

An exit status of 0 means promotion is eligible; 1 means a comparison gate failed.
The supplied coordinated candidate currently exits **1**, because three rejected
attack cases cost more than the baseline. Its quality and safety checks pass.
The report records the failure; no workflow changes automatically.

Shadow replay is an explicit offline facility, not a live traffic interceptor.
It only constructs fixed fake-model/read-tool implementations and a non-writing
ticket simulator. It receives no database, filesystem/browser tool, API key or
external effect adapter. Both replay roles use independent budgets and fresh
context variables, so the caller's checkpoint, ticket gate, memory reader and
budget cannot enter candidate execution. One candidate worker consumes a bounded
queue (default two); its five-second deadline includes queue wait. Total admitted
comparisons are bounded, and owned tasks/futures are cancelled and awaited on close.
Only `Pair.user_result`, the primary result, represents the caller's result.

Each pair consumes an immutable, validated snapshot with the same incident,
read fixtures and initial private-memory facts. Admission checks submit and, when
recalling, memory-read permissions, identity/tenant/service/expiry and a 4 KiB memory bound; project
sharing is not supported by this replay snapshot. Protected content is rejected
before candidate admission. Two credential-bearing fixtures are excluded from
paired replay; all 13 reviewed cases still run in each full evaluator. Raw
rejected payloads and exception messages are omitted from the report.

Workflow pins cover the existing workflow/tool/budget/checkpoint/approval/memory
source hashes and the shadow policy source. A source mismatch rejects replay or
release selection. Input digests correlate pairs without exporting raw inputs.
Reports include explicit evidence, outcome, latency, token, estimated-cost and
quality differences. Prices are synthetic $1/$2 per million input/output tokens;
input accounting uses conservative UTF-8 estimates. These are not real invoices
or measurements of live-provider quality.

Promotion requires every gate:

| Gate | Threshold |
| --- | --- |
| Full fixture evaluation | Both workflows pass all 13 cases / 68 assertions |
| Paired quality/safety | Every applicable assertion passes |
| Outcome/evidence | Exact result and outcome parity on eligible pairs |
| Cost | Candidate estimated cost at most 105% of primary for every pair |
| Latency | Candidate at most 2x primary plus 50 ms for every pair |

Latency samples are illustrative and do not replace the separate load gates.
Local reports and selection files are operator-controlled review artifacts, not
signed attestations. Do not accept them from remote callers or edit failed gates
to authorize a release.

After reviewing a passing report, these are explicit local controls:

```powershell
uv run --no-sync python -m runweave.evaluation.shadow --selection target/evals/selection.json --promote target/evals/shadow.json
uv run --no-sync python -m runweave.evaluation.shadow --selection target/evals/selection.json --check-selection
uv run --no-sync python -m runweave.evaluation.shadow --selection target/evals/selection.json --rollback
```

Promotion validates the current primary/candidate source pins, fixture digest and
all required gates; rollback restores the recorded previous pin only if its source
is available. The CLI does not edit server configuration. For deployment, drain
all jobs and waiting approvals first, stop workers, use the checked selection's
printed workflow as `RUNWEAVE_AGENT_WORKFLOW`, and restart. A workflow switch cannot
resume jobs pinned to another workflow; retain the matching old worker version
until its work is resolved. Preserve old artifacts for rollback. Re-evaluate after
source changes; never rewrite an old pin to bypass a mismatch.

## Load and fault reproduction

```powershell
uv run --no-sync python -m benchmarks.load --output target/evals/sweep.json
uv run --no-sync python -m benchmarks.load --workers 100 --soak-seconds 600 --output target/evals/soak.json
uv run --no-sync python -m runweave.evaluation.suite --workflow baseline --output target/evals/baseline.json
uv run --no-sync python -m runweave.evaluation.suite --workflow pydanticai --output target/evals/pydanticai.json
uv run --no-sync python -m runweave.evaluation.suite --workflow coordinated --output target/evals/coordinated.json
```

The mock sweep compares 1/10/50/100/128 workers on identical batches. The fault
matrix covers 429, transient 503, disconnect, permanent 403, malformed tool data,
timeout and cancellation; unit tests additionally cover invalid model requests,
saturation, deadline races, supervisor failure and shutdown. PostgreSQL tests
cover stale claims, crash windows, approvals, memory authorization and migration.

## Recorded local evidence: November 18, 2026

| Check | Evidence |
| --- | --- |
| Local verification counts, limits and artifact hashes | [release-verification.json](../evaluations/release-verification.json) |
| Durable restart, recall, approval and shadow | [release-demo.json](../evaluations/release-demo.json) |
| Paired comparison and blocked promotion | [shadow-comparison.json](../evaluations/shadow-comparison.json) |
| Native Uvicorn/TCP startup and graceful stop | [native-tcp.json](../evaluations/native-tcp.json) |
| Main service image, real HTTP and exit 0 | [container-smoke.json](../evaluations/container-smoke.json) |
| Independent Python/browser kernel verification | [isolation-native.json](../evaluations/isolation-native.json) |
| Sequential/concurrent sweep | [sweep-m16-windows.json](../benchmarks/sweep-m16-windows.json) |
| Ten-minute soak and memory samples | [soak-m16-windows.json](../benchmarks/soak-m16-windows.json) |

The Windows 11 / Python 3.13.9 host has 16 logical CPUs and 15,791 MiB RAM. The
100-worker sweep completed 128 runs in 1.366 s versus 55.604 s with one worker.
It reached 100 overlapping jobs and tool steps. Status API p95 was 2.679 ms, while
the loop-lag p99 histogram bucket ended at 250 ms. The 601.333 s soak completed
56,960 runs at 94.72 runs/s, retained 128 records, and used 78.22 MiB starting RSS,
84.54 MiB peak and 83.45 MiB ending RSS. Status p95 was 2.239 ms; lag p99 was in the
bucket ending at 1,000 ms. The driver and application share a process and use
mock HTTP transports; other local verification/build activity occurred during
the soak. Connection-pool/network capacity is not measured by that harness.

The strict **event-loop lag p99 below 50 ms remains unmet**, without changing its
threshold. Native TCP and container smoke checks close startup/transport gaps,
but do not establish production load capacity. Hosted CI and live-model quality
remain unverified. The two CI workflows reproduce ordinary and opt-in native
checks; no live inference is enabled in either.

## Wheel and clean-machine check (milestone 22)

A wheel carries the built workspace UI, the project templates and the vendored TypeScript
SDK, so `uvx runweave` works without Node (Node is needed only for TypeScript agents):

```bash
npm --prefix frontend ci
npm --prefix frontend run build
python scripts/bundle-workspace-ui.py      # copies frontend/dist into src/runweave/workspace_ui/
uv build --wheel
```

`src/runweave/workspace_ui/` is not committed; a source checkout serves `frontend/dist`
(or `--assets`). `scripts/check-release.py` does the build and then, outside the
repository with no RunWeave settings or provider keys, runs
`uvx --from <wheel> runweave` to create Python and TypeScript projects, run eval, bench and
shadow, serve the workspace (UI and assets from the wheel) and run an eval and a load test
through the jobs API the screens use. CI runs it on Linux and Windows (`clean-install`).

Publishing to PyPI (Trusted Publishing, a 0.x pre-release) happens only when the owner asks.

## Service image and Compose

`docker build .` builds the UI in a Node stage, bundles it into the package, and installs
RunWeave with the `durable` and `workflow` extras, the migrations and `alembic.ini`. The
image runs as `10001:10001` with `runweave serve --container`: the only difference from
`runweave serve` is that it listens on the container's interfaces. Publish it on the
host's loopback only; the workspace still accepts only the `127.0.0.1` host and its own
origin, and provider keys come from the environment (`RUNWEAVE_KEY_STORE=none`).

`compose.yaml` runs PostgreSQL 18 (not published to the host, data in a named volume), a
one-shot migration, then RunWeave on `127.0.0.1:${RUNWEAVE_PORT:-8000}`, with a read-only
root filesystem, no capabilities and `no-new-privileges`. The database password lives in
`compose.env` (copy `compose.env.example`; it is git-ignored and separate from RunWeave's
`.env`, which rejects unknown keys):

```powershell
Copy-Item compose.env.example compose.env   # then set POSTGRES_PASSWORD
docker compose --env-file compose.env up --build
docker compose --env-file compose.env down     # add -v to delete the database volume
```

To run your own Python agents in it, build an image `FROM` this one with your package
installed and set `RUNWEAVE_AGENTS`.

`python scripts/check-container.py --compose --image <image> --output <file>` brings the
stack up under a unique project name and a free port, then checks:
- only RunWeave is published, on `127.0.0.1`;
- the UI is served;
- a spoofed Host gets 400 and a cross-origin request 403;
- a durable triage run waits for approval, is approved and succeeds;
- the run is still there after RunWeave restarts.

It then removes the stack and its volume. The release workflow runs it on every pull
request.

## Releases (owner only)

`.github/workflows/release.yml` builds on every pull request:
- the wheel and sdist, with the UI bundled;
- the TypeScript SDK tarball;
- CycloneDX SBOMs for the Python package, the workspace UI and the SDK;
- `SHA256SUMS`;
- the service image, checked through Compose.

These are kept as workflow artifacts. Nothing is published from a pull request.

Publishing runs only when the owner pushes a tag (`vX.Y.Z` or `vX.Y.Z-rc.N`, matching
`pyproject.toml` and the SDK's `package.json`) or starts the workflow by hand for an
existing tag. All three publishing jobs run in the `release` environment:
- `publish-npm`: `@runweave/agent` with provenance, through npm trusted publishing (no
  token stored);
- `publish-image`: pushes the exact image that was checked to
  `ghcr.io/<owner>/runweave:<version>`;
- `draft-release`: verifies the checksums, adds the desktop files, and creates a **draft**
  GitHub Release with every file:
  - the unsigned desktop installers: MSI and NSIS, DMG, `.deb`, `.rpm` and AppImage;
  - `THIRD_PARTY_NOTICES.txt`;
  - the Rust crates' SBOM (`runweave-desktop-rust.cdx.json`).

  `SHA256SUMS` is rebuilt to cover all of them.

For the tag, the `desktop` job runs `.github/workflows/desktop.yml` as a reusable
workflow: it builds the installers on Windows, macOS and Linux, then installs, uses and
uninstalls each one on a fresh runner (see [Desktop app](desktop.md#checked-on-clean-machines)).
The draft is created only after those checks pass.

One-time setup by the owner, before the first release:
- create the `release` environment with yourself as required reviewer, limited to `v*`
  tags;
- add this repository and workflow as a trusted publisher of `@runweave/agent` on
  npmjs.com.

Actions are pinned by commit SHA, the workflow has no default permissions, and each job
gets only what it needs. CI also runs actionlint (with shellcheck) on every workflow. PyPI
publishing is not part of this workflow.

## Load tests with the workspace open

`scripts/measure-ui-overhead.py` alternates the same `runweave bench` from a terminal and
from the Load tests screen's API while a client polls the workspace as the open screen
does. On Windows-11-10.0.26300-SP0 (16 logical CPUs,
Python 3.13.9), with an agent that awaits 50 ms per run, 300 runs per
level and 5 alternating rounds
([ui-overhead-windows.json](../benchmarks/ui-overhead-windows.json)):

| Concurrency | Status API p95, CLI / UI (median ms) | Run p95, CLI / UI (ms) | Runs/s, CLI / UI |
| ---: | --- | --- | --- |
| 8 | 33.5 / 33.4 | 65.8 / 61.8 | 100.5 / 102.1 |
| 64 | 716.0 / 804.0 | 89.5 / 90.7 | 88.99 / 92.52 |

No measurable regression: at concurrency 8 the medians match, and at 64 the per-round
status-API p95 ranges overlap (CLI 689.1–1144.2 ms,
UI 696.7–1067.7 ms). The job runs in its own process, so the screen's polling
lands on the workspace, not on the RunWeave being measured. The poller stands in for a
browser (charts render client-side). The high status-API p95 at 64 is the load test's own
in-process driver competing for one event loop, and throughput near 90 runs/s is the
per-run CPU cost of RunWeave and the driver sharing one process on this machine, with or
without the UI; neither is a claim about provider capacity.
