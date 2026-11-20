# Desktop app

The desktop app (milestone 25) is a Tauri window around the workspace, with RunWeave
running beside it as a frozen Python program: the **desktop service**. Installers and
first-launch steps for unsigned builds come with the release pull request of milestone 25.

## The window

On first launch the window shows a setup page:
- **Project folder:** pick a folder with a `runweave.toml` (`runweave init my-agent` creates
  one).
- **Python interpreter:** for Python agents, pick the `python` of the environment your
  agent runs in, with `runweave` installed. HTTP agents don't need one.

**Start RunWeave** saves both choices in the app's config folder (`desktop.json`), starts
the service, and shows the workspace once it is ready. The first start sets up the local
database and takes longer. Later launches start straight away with the saved choices. If
the service cannot start, the page shows why, for example the exact command to install
`runweave` in your interpreter, and lets you choose again.

Quitting the window closes the service's input, so it stops your agent and PostgreSQL. It
is killed only if it hasn't stopped after 20 seconds. If the window itself is killed, the
service still sees its input close and shuts down the same way.

### What the window allows

- **App commands:** only the bundled setup page can call them, and there are four:
  - read the saved choices;
  - choose a folder;
  - choose an interpreter;
  - start.

  They are granted by `capabilities/default.json`, and the build requires every command to
  be granted explicitly.
- **The workspace:** it is a page served by the service on `127.0.0.1`. It has no access to
  those commands, and the window navigates only to the setup page and that one origin.
- **Restrictions:**
  - no shell, file system or updater plugins;
  - the folder and interpreter pickers run in Rust, not in the page;
  - auto-update is off;
  - the setup page has its own strict CSP.

### Build it from a checkout

You need Rust (the version is pinned in `desktop/src-tauri/rust-toolchain.toml`) and Node 22
or newer. Windows also needs the MSVC build tools; Linux needs WebKitGTK 4.1 (see
`.github/workflows/desktop.yml`). Then:

```powershell
uv run --no-sync python scripts/fetch-postgres.py --out target/postgres --archive target/postgres-archive/postgres.txz
uv run --no-sync python scripts/build-sidecar.py
npm.cmd --prefix desktop ci
npm.cmd --prefix desktop run build
```

The installers land in `desktop/src-tauri/target/release/bundle/`:
- Windows: MSI and NSIS;
- macOS: `.app` and DMG;
- Linux: `.deb`, `.rpm` and AppImage.

They bundle the frozen service (`sidecar/`) and the PostgreSQL archive (`postgres/`) as
resources. `cargo test` in `desktop/src-tauri` runs the window's own tests: the saved
choices, and the service lifecycle against a fake service.

## What the service does

`runweave-desktop-service` is what the window starts. It:

1. **Unpacks PostgreSQL** on first start: PostgreSQL 18.7 from the bundled archive into
   the app-data folder (once per archive checksum).
2. **Starts PostgreSQL** on `127.0.0.1` and a free port:
   - no Unix sockets;
   - SCRAM authentication for one `runweave` role;
   - the password is generated at first start.
3. **Applies the database migrations.**
4. **Connects your project's agent:**
   - **A Python agent** (`python = "module:function"` in `runweave.toml`) runs with the
     interpreter you choose, through `<python> -m runweave.cli serve-agent`. The service
     connects to it as an HTTP agent on loopback. Your interpreter needs `runweave`
     installed, at the same minor version as the app. If it isn't, the service stops with
     the exact install command.
   - **An HTTP agent** connects directly.
   - Eval, load-test and shadow jobs also run with your interpreter.
5. **Serves the workspace** on another free loopback port, prints
   `RUNWEAVE_DESKTOP_READY <url>`, and keeps the workspace's Host and origin checks.

**Shutdown:** the service stops when its standard input closes, which happens when the
window quits or dies, or on Ctrl+C or SIGTERM. It then stops your agent process and shuts
PostgreSQL down (fast shutdown). Errors go to stderr as one line starting with
`RUNWEAVE_DESKTOP_ERROR`.

### Its files

Everything lives in the state folder the window passes as `--state-dir`:

| Path | What |
| --- | --- |
| `postgres/binaries-<checksum>/` | The unpacked PostgreSQL build |
| `postgres/data/` | The database. PostgreSQL requires that only you can read it |
| `postgres/password` | The generated database password (mode 0600 on macOS and Linux) |
| `postgres/postgres.log` | The server log |

The password file sits beside a data folder that only you can read. Anyone who can read
the file can already read the data, so the OS credential store would add nothing.

## Run it from a checkout

```powershell
uv sync --locked --extra durable --extra workflow --group desktop
uv run --no-sync python scripts/fetch-postgres.py --out target/postgres --archive target/postgres-archive/postgres.txz
uv run --no-sync runweave-desktop-service --state-dir target/desktop-state --postgres target/postgres --project path\to\runweave.toml --python path\to\your\venv\Scripts\python.exe
```

`scripts/fetch-postgres.py` downloads the pinned build for your platform from Maven
Central and checks its SHA-256. The pinned platforms are Windows x64, Linux x64 and
arm64, and macOS x64 and arm64.

## Frozen build

```powershell
npm.cmd --prefix frontend ci
npm.cmd --prefix frontend run build
uv run --no-sync python scripts/build-sidecar.py
```

This builds a one-folder PyInstaller program in `target/sidecar/runweave-desktop-service/`,
and writes `build.json` (the Rust target triple) for the Tauri build. It then
smoke-tests the result:
- starts it against the pinned PostgreSQL archive;
- checks that the workspace is served;
- checks that a durable run succeeds;
- closes stdin;
- requires exit code 0 and no leftover processes.

CI does this on Windows, Linux and macOS (`.github/workflows/desktop.yml`).

## Limits

- **Your agent code is trusted.** It runs with your interpreter and your permissions, as
  with `runweave serve`.
- **A hard-killed service can leave PostgreSQL running.** A graceful quit stops
  everything, but if the service is killed (not closed) PostgreSQL keeps running until the
  next start, which stops it first.
- **The first start is slower** because PostgreSQL is unpacked and initialized: about
  20 s on the Windows machine used for development.
- **Nothing is signed yet,** and there is no auto-update. Windows SmartScreen and macOS
  Gatekeeper warn about unsigned apps; the release notes will give first-launch steps.
