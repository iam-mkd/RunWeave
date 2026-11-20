"""Freeze the desktop service with PyInstaller and smoke-test the result.

    npm --prefix frontend ci && npm --prefix frontend run build
    uv sync --locked --extra durable --extra workflow --group desktop
    uv run --no-sync python scripts/build-sidecar.py

Writes a one-folder build to target/sidecar/runweave-desktop-service/ (the shell spawns
`runweave-desktop-service[.exe]` inside it) and target/sidecar/build.json with the Rust
target triple, so the Tauri build can place it. With --smoke (the default) it then starts
the frozen service against the pinned PostgreSQL archive, checks the workspace answers,
closes stdin and expects a clean exit with no processes left behind.
"""

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NAME = "runweave-desktop-service"
OUT = ROOT / "target" / "sidecar"


def target_triple() -> str:
    arch = "aarch64" if platform.machine().lower() in ("arm64", "aarch64") else "x86_64"
    return {
        "win32": f"{arch}-pc-windows-msvc",
        "darwin": f"{arch}-apple-darwin",
        "linux": f"{arch}-unknown-linux-gnu",
    }[sys.platform]


def build() -> Path:
    subprocess.run([sys.executable, str(ROOT / "scripts" / "bundle-workspace-ui.py")], check=True)
    separator = os.pathsep
    entry = ROOT / "target" / "sidecar-entry.py"
    entry.parent.mkdir(parents=True, exist_ok=True)
    entry.write_text("from runweave.desktop.service import main\n\nmain()\n", encoding="utf-8")
    work = ROOT / "target" / "sidecar-work"
    subprocess.run(
        [
            sys.executable, "-m", "PyInstaller", str(entry), "--name", NAME, "--onedir",
            "--noconfirm", "--clean", "--console", "--log-level", "WARN",
            "--distpath", str(OUT), "--workpath", str(work), "--specpath", str(work),
            "--add-data", f"{ROOT / 'migrations'}{separator}migrations",
            # Imported by name at run time (agents, dialects, ASGI server, workflow).
            "--collect-submodules", "runweave",
            "--collect-data", "runweave",
            "--collect-submodules", "uvicorn",
            "--collect-submodules", "alembic",
            "--collect-data", "alembic",
            "--hidden-import", "sqlalchemy.dialects.postgresql.asyncpg",
            "--collect-submodules", "asyncpg",
            "--collect-submodules", "keyring.backends",
            "--copy-metadata", "runweave",
            "--exclude-module", "tkinter",
        ],
        check=True,
        cwd=ROOT,
    )  # fmt: skip
    folder = OUT / NAME
    (OUT / "build.json").write_text(
        json.dumps(
            {"target": target_triple(), "folder": NAME, "python": platform.python_version()},
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return folder


def smoke(folder: Path, archive: Path) -> dict[str, object]:
    import httpx
    import psutil  # type: ignore[import-untyped]

    executable = folder / (NAME + (".exe" if sys.platform == "win32" else ""))
    env = {k: v for k, v in os.environ.items() if not k.startswith(("RUNWEAVE_", "PYTHON"))}
    env["RUNWEAVE_KEY_STORE"] = "memory"
    with tempfile.TemporaryDirectory() as state:
        started = time.monotonic()
        service = subprocess.Popen(
            [str(executable), "--state-dir", state, "--postgres-archive", str(archive)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
            text=True,
        )  # fmt: skip
        try:
            assert service.stdout is not None and service.stdin is not None
            ready = service.stdout.readline()
            if not ready.startswith("RUNWEAVE_DESKTOP_READY "):
                errors = service.stderr.read() if service.stderr else ""
                raise SystemExit(f"The frozen service did not start: {ready}{errors}")
            startup = time.monotonic() - started
            base = ready.split()[1]
            children = [child.pid for child in psutil.Process(service.pid).children(recursive=True)]
            with httpx.Client(base_url=base, trust_env=False, timeout=10) as client:
                assert client.get("/health/ready").status_code == 200
                assert client.get("/v1/workspace").json()["durable"] is True
                page = client.get("/")
                assert page.status_code == 200 and '<div id="root">' in page.text
                admitted = client.post(
                    "/v1/runs", json={"service": "payments", "description": "Frozen smoke"}
                )
                assert admitted.status_code == 202, admitted.text
                for _ in range(300):
                    run = client.get(admitted.headers["Location"]).json()
                    if run["finished_at"]:
                        break
                    time.sleep(0.05)
                assert run["status"] == "SUCCEEDED", run
            service.stdin.close()
            code = service.wait(timeout=60)
        finally:
            if service.poll() is None:
                service.kill()
                service.wait()
        time.sleep(1)
        leftovers = [pid for pid in children if psutil.pid_exists(pid)]
    assert code == 0, code
    assert leftovers == [], leftovers
    return {
        "passed": True,
        "startup_seconds": round(startup, 1),
        "exit_code": code,
        "leftover_processes": 0,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--postgres-archive",
        type=Path,
        default=ROOT / "target" / "postgres-archive" / "postgres.txz",
    )
    parser.add_argument("--no-smoke", action="store_true")
    args = parser.parse_args()
    if OUT.exists():
        shutil.rmtree(OUT)
    folder = build()
    size = sum(path.stat().st_size for path in folder.rglob("*") if path.is_file())
    print(f"Built {folder} ({size // (1024 * 1024)} MiB) for {target_triple()}")
    if not args.no_smoke:
        result = smoke(folder, args.postgres_archive)
        (OUT / "smoke.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        print(f"Smoke test passed: {result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
