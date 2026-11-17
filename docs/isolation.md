# Disposable Python and browser tools

Milestone 15 has two independently reviewable workers: a restricted Python data
tool and a read-only Playwright Chromium tool. They are explicit typed API tools;
the incident-triage model's existing tool registry remains unchanged. Normal CI
and the offline demo never start Docker or call a paid provider.

## Reproduce the native verification

Start Docker Desktop with the Linux engine and cgroup v2, then run from PowerShell:

```powershell
./scripts/verify-isolation.ps1
```

The script builds pinned worker images, resolves immutable image IDs, runs the
native tests, and demonstrates both tools. It writes startup/kernel evidence to
`target/isolation/native-proof.json` and the demonstration to
`target/isolation/demo-proof.json`. Each container is removed by ownership label
and unique name. Images are retained for reuse. No host directory or Docker socket
is mounted into a worker. Build downloads require network access; worker execution
has no network access. The browser image contains its own pinned Playwright
package and matching browsers; no host Playwright dependency is needed.
The API and Python worker use Python 3.13. The pinned upstream browser image uses
its own Python 3.12 interpreter solely for the disposable Playwright worker.

To enable the API, copy the IDs reported by `docker image inspect` into your
local environment:

```powershell
$env:RUNWEAVE_ISOLATED_TOOLS_ENABLED = 'true'
$env:RUNWEAVE_SANDBOX_PYTHON_IMAGE = docker image inspect --format '{{.Id}}' runweave-python:m15
$env:RUNWEAVE_SANDBOX_BROWSER_IMAGE = docker image inspect --format '{{.Id}}' runweave-browser:m15
$env:RUNWEAVE_BROWSER_ALLOWED_ORIGINS = '["https://fixtures.runweave.invalid"]'
$env:RUNWEAVE_MODEL_PROVIDER = 'fake'
uv run --locked uvicorn runweave.app:create_app --factory --workers 1
```

Only trusted images built from this checkout should be configured. Immutable IDs
prevent mutable-tag substitution; protocol labels are compatibility checks, not
image signatures. Startup verifies actual kernel controls and resource exhaustion
before enabling either endpoint. An unsupported runtime or failed probe aborts
startup. Uncertain cleanup disables the tools and fails readiness. Disabled tools
return 503 without contacting Docker. Admission allows at most twice the configured
concurrency in active plus waiting calls (default two active and two waiting).

## API and permitted work

```powershell
Invoke-RestMethod -Method Post http://127.0.0.1:8000/v1/tools/python `
  -ContentType application/json -Body '{"code":"result=sum(x*x for x in input_data)","data":[1,2,3]}'
Invoke-RestMethod -Method Post http://127.0.0.1:8000/v1/tools/browser `
  -ContentType application/json -Body '{"url":"https://fixtures.runweave.invalid/"}'
```

Python accepts JSON `data` as `input_data` and returns the JSON variable `result`.
The AST policy permits bounded syntax for calculations, loops and comprehensions.
Imports, attributes, reflection, filesystem functions, arbitrary builtins and shell
access are rejected before execution and checked again inside the worker. Infinite
loops and allocation bombs are stopped by independent resource limits.

The browser navigates only the approved synthetic origin and fixed fixture paths.
A host-side in-memory HTTP fixture broker supplies a bounded resource bundle;
Chromium has no network interface beyond loopback. Routing permits only GETs to
bundle entries, rejects redirects and foreign subresources, and blocks service
workers and WebSockets. Fresh container, browser and context state prevent cookie
or storage sharing. Returned page text is untrusted data and never grants tools
or changes policy. This stage does not fetch arbitrary Internet sites.

Scopes are `tools:python_sandbox`, `tools:browser_read`, and additionally
`tools:browser_private` for `/private`. Configure a synthetic
`RUNWEAVE_SANDBOX_FIXTURE_CREDENTIAL` to use the private fixture. The broker checks
the current principal and tenant and supplies only that tenant's fixture response.
The credential stays in the host broker; neither worker environment, browser
headers, page content nor tool results receive it. Revocations are checked again
after waiting for a slot. Local authentication grants development scopes; use
the existing authenticated mode and deployment gate before any shared exposure.

Browser writes are denied, including POSTs from page scripts. The request schema
accepts no method, script, credentials or approval overrides. Milestone 12's
controlled ticket action remains the only effect adapter, with its approval and
idempotency/reconciliation rules. Adding a browser write adapter requires the same
durable approval binding and downstream effect protocol; a page instruction or an
approval-looking string cannot authorize one.

## Enforced limits

| Control | Python | Browser |
| --- | --- | --- |
| Wall deadline after creation | 8 s | 15 s |
| CPU quota / CPU time limit | 0.5 CPU / 1 s | 1 CPU / 10 s |
| Container memory; swap disabled | 64 MiB | 768 MiB |
| Process count | 16 | 128 |
| Writable tmpfs | 1 MiB | 128 MiB |
| Per-file size | 64 KiB | 8 MiB |
| Open file descriptors | 32 | 256 |
| API request | 16 KiB | 16 KiB |
| Result | 4 KiB JSON | 4 KiB page text, 32 request URLs |
| Transport stdout/stderr | 16 KiB each | 16 KiB each |

Docker CLI operations have a separate 30 s timeout. Creation and cleanup are
owned and awaited even after cancellation; cleanup can therefore extend beyond
the execution deadline. Worker roots are read-only, users non-root, capabilities
empty, no-new-privileges enabled, core dumps disabled and IPC private. Chromium
launches with `chromium_sandbox=True`; there is no unsandboxed fallback.

These are Linux container boundaries sharing the Docker VM kernel, not a claim of
VM-grade isolation against kernel or browser vulnerabilities. The host controller
has Docker authority and must remain trusted. Do not mount its socket into the
public application container or expose Docker remotely as a shortcut. A separate
least-privilege controller would be needed for a broader hostile-workload service.

The browser seccomp profile derives from [Playwright v1.64.1](https://github.com/microsoft/playwright/blob/v1.64.1/utils/docker/seccomp_profile.json).
One explicit `chroot` syscall allowance lets Chromium chroot inside its own user
namespace with all outer capabilities dropped. Native probes verify that the
outer worker still cannot chroot. Startup verifies the canonical JSON SHA-256
`77cdaf463cf5a605278debc2cfe13d5b09c1d5331fc3d4599b8f9466741c85e4`.
See [Playwright's container guidance](https://playwright.dev/python/docs/docker)
and [Docker resource controls](https://docs.docker.com/engine/containers/run/).

## Verification scope

The native suite checks kernel controls, CPU/memory/process/file/output exhaustion,
path and environment isolation, network denial, browser redirects/subresources,
malicious pages, tenant credential separation, cookie/storage separation, API
results, and cleanup after timeout/cancellation. Unit tests cover syntax and URL
policy, disabled startup, scoped permissions, bounded admission, shutdown and
uncertain/foreign-owner cleanup. Native tests skip unless both
`TEST_SANDBOX_*_IMAGE` variables are supplied. Run these independently of ordinary
tests and the durable PostgreSQL suite. See PLAN.md for the recorded local result;
hosted CI and broader release/load verification remain milestone 16 work.
