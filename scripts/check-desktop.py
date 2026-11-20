"""Install the desktop app from its installer, use it, quit, uninstall, and check nothing is left.

    python scripts/check-desktop.py --installer <file> --python <interpreter> --output <report.json>

For a clean machine (CI runs it on fresh Windows, macOS and Linux runners). `--python` is
the developer's interpreter: it needs `runweave` and the sample agent installed
(`examples/sample-agent`). Steps:

1. Install: NSIS silently (Windows), copy the app out of the DMG (macOS), apt (Linux).
2. First launch, with saved choices for a `runweave init` project: the window starts the
   service (unpacking PostgreSQL the first time); an eval job, a load-test job and a run
   go through the workspace's API, as the screens send them. Quit like a user would.
3. Second launch, for a project whose agent asks for approval: the run waits, is approved
   through the Approvals API, and succeeds. Quit.
4. After each quit, and after uninstalling: no app, service, agent or PostgreSQL process
   is left, and PostgreSQL shut down cleanly.

It refuses to run if the app already has data on this machine, unless --replace-data is
given, because it removes the app's folders at the end.
"""

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import psutil  # type: ignore[import-untyped]

APP_ID = "dev.runweave.desktop"
SERVICE = "runweave-desktop-service"
REFUND_AGENT = 'name = "refund-request"\npython = "sample_agent:refund_request"'


def app_folders(platform: str, home: Path, env: dict[str, str]) -> tuple[Path, Path]:
    """The app's config and data folders, as Tauri resolves them on each platform."""
    if platform == "win32":
        return Path(env["APPDATA"]) / APP_ID, Path(env["LOCALAPPDATA"]) / APP_ID
    if platform == "darwin":
        support = home / "Library" / "Application Support" / APP_ID
        return support, support
    config = Path(env.get("XDG_CONFIG_HOME") or home / ".config")
    data = Path(env.get("XDG_DATA_HOME") or home / ".local" / "share")
    return config / APP_ID, data / APP_ID


@dataclass
class Installed:
    executable: Path
    uninstall: Callable[[], object]
    removed: Callable[[], bool]


def _run(*argv: str, timeout: float = 600, answer: str | None = None) -> str:
    done = subprocess.run(
        argv, capture_output=True, text=True, timeout=timeout, check=False, input=answer
    )
    if done.returncode != 0:
        raise SystemExit(f"{' '.join(argv)} failed ({done.returncode}): {done.stderr[-2000:]}")
    return done.stdout


def _until(check: Callable[[], Any], what: str, seconds: float) -> Any:
    deadline = time.monotonic() + seconds
    while True:
        value = check()
        if value:
            return value
        if time.monotonic() > deadline:
            raise SystemExit(f"Timed out after {seconds:.0f} s waiting for {what}")
        time.sleep(0.5)


if sys.platform == "win32":
    import winreg

    def _uninstall_entry() -> dict[str, str] | None:
        """RunWeave's entry among the registry's installed programs, if any."""
        key_path = r"Software\Microsoft\Windows\CurrentVersion\Uninstall"
        for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                with winreg.OpenKey(root, key_path) as keys:
                    for index in range(winreg.QueryInfoKey(keys)[0]):
                        with winreg.OpenKey(keys, winreg.EnumKey(keys, index)) as key:
                            values = {}
                            for name in ("DisplayName", "InstallLocation", "UninstallString"):
                                try:
                                    values[name] = str(winreg.QueryValueEx(key, name)[0])
                                except OSError:
                                    pass
                            if values.get("DisplayName") == "RunWeave":
                                return values
            except OSError:
                continue
        return None

else:

    def _uninstall_entry() -> dict[str, str] | None:
        raise RuntimeError("The Windows registry exists only on Windows")


def _install_windows(installer: Path) -> Installed:
    _run(str(installer), "/S")
    found = _until(_uninstall_entry, "the installer to register RunWeave", 120)
    folder = Path(found["InstallLocation"].strip('"'))
    [executable] = [
        item for item in folder.glob("*.exe") if not item.name.lower().startswith("uninstall")
    ]
    uninstaller = found["UninstallString"].strip('"')
    return Installed(
        executable,
        lambda: _run(uninstaller, "/S"),
        lambda: not executable.exists() and _uninstall_entry() is None,
    )


def _install_macos(installer: Path, into: Path) -> Installed:
    mount = Path(tempfile.mkdtemp(prefix="runweave-dmg-"))
    # Without a terminal hdiutil cancels an attach that wants confirmation (a license page or
    # verification prompt), so answer it and skip the interactive steps; retry a busy disk.
    attach = ("hdiutil", "attach", "-nobrowse", "-readonly", "-noverify", "-noautoopen",
              "-mountpoint", str(mount), str(installer))  # fmt: skip
    for attempt in range(3):
        try:
            _run(*attach, answer="Y\n")
            break
        except SystemExit:
            if attempt == 2:
                print(_run("hdiutil", "imageinfo", str(installer)), file=sys.stderr)
                raise
            time.sleep(5)
    try:
        [bundle] = list(mount.glob("*.app"))
        app = into / bundle.name
        _run("cp", "-R", str(bundle), str(app))
    finally:
        _run("hdiutil", "detach", str(mount))
    [executable] = list((app / "Contents" / "MacOS").iterdir())
    return Installed(executable, lambda: shutil.rmtree(app), lambda: not app.exists())


def _install_linux(installer: Path) -> Installed:
    package = _run("dpkg-deb", "-f", str(installer), "Package").strip()
    _run("sudo", "apt-get", "install", "-y", "--no-install-recommends", str(installer.resolve()))
    [executable] = [
        Path(line)
        for line in _run("dpkg", "-L", package).splitlines()
        if line.startswith("/usr/bin/")
    ]

    def installed() -> bool:
        status = subprocess.run(["dpkg", "-s", package], capture_output=True, check=False)
        return status.returncode == 0

    return Installed(
        executable,
        lambda: _run("sudo", "apt-get", "remove", "-y", package),
        lambda: not installed() and not executable.exists(),
    )


def install(installer: Path, work: Path) -> Installed:
    if sys.platform == "win32":
        return _install_windows(installer)
    if sys.platform == "darwin":
        return _install_macos(installer, work)
    return _install_linux(installer)


def make_project(python: Path, folder: Path, *, refund: bool = False) -> Path:
    _run(str(python), "-m", "runweave.cli", "init", str(folder))
    config = folder / "runweave.toml"
    if refund:
        text = config.read_text(encoding="utf-8")
        config.write_text(
            text.replace('name = "word-count"\npython = "agent:run"', REFUND_AGENT),
            encoding="utf-8",
        )
    return config


def _tree(pid: int) -> list[psutil.Process]:
    try:
        return list(psutil.Process(pid).children(recursive=True))
    except psutil.Error:
        return []


def _service_url(app: subprocess.Popen[bytes]) -> str | None:
    if app.poll() is not None:
        raise SystemExit(f"The app exited early with code {app.returncode}")
    for child in _tree(app.pid):
        try:
            if not child.name().startswith(SERVICE):
                continue
            ports = [c.laddr.port for c in child.net_connections("tcp") if c.status == "LISTEN"]
        except psutil.Error:
            continue
        for port in ports:
            try:
                if httpx.get(f"http://127.0.0.1:{port}/v1/workspace", timeout=2).status_code == 200:
                    return f"http://127.0.0.1:{port}"
            except httpx.HTTPError:
                pass
    return None


def _name(process: psutil.Process) -> str:
    try:
        return str(process.name())
    except psutil.Error:
        return ""


def _alive(processes: list[psutil.Process]) -> list[str]:
    left = []
    for process in processes:
        try:
            if process.is_running() and process.status() != psutil.STATUS_ZOMBIE:
                left.append(f"{process.name()} ({process.pid})")
        except psutil.Error:
            pass
    return left


def _app_processes() -> list[str]:
    """The service, and any PostgreSQL running from the app's folders.

    Matched by the app's identifier rather than a full path: Windows may redirect the
    app-data folder (for example for programs started from a packaged app).
    """
    found = []
    for process in psutil.process_iter(["name", "cmdline"]):
        try:
            name = process.info["name"] or ""
            command = " ".join(process.info["cmdline"] or [])
            if name.startswith(SERVICE) or ("postgres" in name and APP_ID in command):
                found.append(f"{process.info['name']} ({process.pid})")
        except psutil.Error:
            pass
    return found


def _gone(still: Callable[[], list[str]], seconds: float) -> list[str]:
    """Waits up to `seconds` for `still()` to be empty; returns what is left."""
    deadline = time.monotonic() + seconds
    while (left := still()) and time.monotonic() < deadline:
        time.sleep(0.5)
    return left


def _quit(app: subprocess.Popen[bytes]) -> None:
    """Close the window as a user does, then wait for the app's processes to finish."""
    if app.poll() is None:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/PID", str(app.pid)], capture_output=True, check=False)
        else:
            app.send_signal(signal.SIGTERM)  # WM_CLOSE on Windows, as the close button sends
        try:
            app.wait(timeout=60)
        except subprocess.TimeoutExpired:
            app.kill()
            app.wait()
    _gone(_app_processes, 90)


def _sidecar(executable: Path) -> Path | None:
    """The bundled service: in Contents/Resources on macOS, beside the window elsewhere."""
    roots = [executable.parent, executable.parent.parent / "Resources",
             Path("/usr/lib") / executable.name, Path("/usr/lib") / "RunWeave"]  # fmt: skip
    for root in roots:
        for found in root.glob(f"sidecar/{SERVICE}*") if root.is_dir() else []:
            if found.is_file():
                return found
    return None


def diagnose(app: subprocess.Popen[bytes], executable: Path, data: Path, choices: Path) -> str:
    """Why the workspace did not come up: the window's processes, then the service run alone."""
    lines = [f"Window {executable} (pid {app.pid}, exit code {app.poll()})"]
    for child in _tree(app.pid):
        try:
            ports = [c.laddr.port for c in child.net_connections("tcp") if c.status == "LISTEN"]
            lines.append(f"  {child.pid} {child.name()} ports={ports} {child.cmdline()}")
        except psutil.Error as error:
            lines.append(f"  {child.pid}: {error!r}")
    if sys.platform == "darwin":
        # What the window shows (the CI job uploads it), and what the app logged.
        screen = Path("target") / "desktop-check-screen.png"  # the job uploads target/
        screen.parent.mkdir(parents=True, exist_ok=True)
        shot = subprocess.run(
            ["screencapture", "-x", str(screen)], capture_output=True, text=True, check=False
        )
        lines.append(f"Screenshot {screen}: exit code {shot.returncode} {shot.stderr.strip()}")
        logged = subprocess.run(
            ["log", "show", "--last", "6m", "--style", "compact",
             "--predicate", 'process BEGINSWITH "runweave"'],
            capture_output=True, text=True, timeout=120, check=False,
        )  # fmt: skip
        lines += ["System log of the app:", *logged.stdout.splitlines()[-150:]]
    sidecar = _sidecar(executable)
    if sidecar is None:
        return "\n".join([*lines, "The bundled service was not found"])
    saved = json.loads(choices.read_text(encoding="utf-8"))
    archive = sidecar.parent.parent / "postgres" / "postgres.txz"
    argv = [str(sidecar), "--state-dir", str(data), "--postgres-archive", str(archive),
            "--project", saved["project"], "--python", saved["python"]]  # fmt: skip
    _quit(app)  # one service at a time: the window's may still hold the data folder
    lines.append(f"Running the service alone: {argv}")
    service = subprocess.Popen(
        argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    try:
        out, err = service.communicate(timeout=120)
    except subprocess.TimeoutExpired:
        service.kill()
        out, err = service.communicate()
    lines += [f"exit code {service.returncode}", "stdout:", out[-4000:], "stderr:", err[-8000:]]
    return "\n".join(lines)


def session(
    executable: Path,
    data: Path,
    choices: Path,
    use: Callable[[httpx.Client], dict[str, Any]],
) -> dict[str, Any]:
    """Launch, wait for the workspace, use it, quit like a user, check what is left."""
    started = time.monotonic()
    app = subprocess.Popen([str(executable)])
    try:
        try:
            base = _until(lambda: _service_url(app), "the window to start the service", 300)
        except SystemExit:
            print(diagnose(app, executable, data, choices), file=sys.stderr)
            raise
        ready = round(time.monotonic() - started, 1)
        with httpx.Client(base_url=base, timeout=30, trust_env=False) as client:
            result = use(client)
        processes = _tree(app.pid)
        names = sorted({name for p in processes if (name := _name(p))})
        _quit(app)
        left = _gone(lambda: _alive(processes), 30) + _gone(_app_processes, 30)
        clean = not (data / "postgres" / "data" / "postmaster.pid").exists()
        log = (data / "postgres" / "postgres.log").read_text(encoding="utf-8", errors="replace")
        return {
            "ready_seconds": ready,
            "processes_seen": names,
            "left_running": left,
            "postgres_shut_down": clean and "database system is shut down" in log.splitlines()[-1],
            **result,
        }
    finally:
        _quit(app)  # also after a failure, so uninstalling never races a shutdown


def _job(client: httpx.Client, request: dict[str, Any]) -> dict[str, Any]:
    started = client.post("/v1/jobs", json=request)
    assert started.status_code == 202, started.text
    job_id = started.json()["id"]

    def finished() -> dict[str, Any] | None:
        job: dict[str, Any] = client.get(f"/v1/jobs/{job_id}").json()["job"]
        return job if job["status"] != "RUNNING" else None

    job = _until(finished, f"the {request['kind']} job", 300)
    assert job["status"] == "COMPLETED" and job.get("passed") is not False, job
    return {"status": job["status"], "report": job.get("report_id")}


def _run_to_end(client: httpx.Client, url: str) -> dict[str, Any]:
    run: dict[str, Any] = _until(
        lambda: (body := client.get(url).json())["finished_at"] and body, "the run", 120
    )
    return run


def first_use(client: httpx.Client) -> dict[str, Any]:
    assert client.get("/v1/workspace").json()["durable"] is True
    assert client.get("/").status_code == 200
    evaluation = _job(client, {"kind": "eval", "dataset": "smoke"})
    bench = _job(
        client,
        {
            "kind": "bench",
            "dataset": "smoke",
            "concurrency": [1, 4],
            "runs": 8,
            "fault_profiles": [""],
            "soak_seconds": 0,
        },
    )
    admitted = client.post(
        "/v1/runs", json={"agent": "word-count", "input": {"text": "one two three"}}
    )
    assert admitted.status_code == 202, admitted.text
    run = _run_to_end(client, admitted.headers["Location"])
    assert run["status"] == "SUCCEEDED", run
    return {"eval": evaluation, "bench": bench, "run": run["status"]}


def approval_use(client: httpx.Client) -> dict[str, Any]:
    admitted = client.post(
        "/v1/runs",
        json={
            "agent": "refund-request",
            "input": {"order": "A-1042", "amount": 40, "note": "Charged twice."},
        },
    )
    assert admitted.status_code == 202, admitted.text
    url = admitted.headers["Location"]
    _until(
        lambda: client.get(url).json()["status"] == "WAITING_APPROVAL",
        "the run to ask for approval",
        120,
    )
    [request] = client.get("/v1/approvals").json()["approvals"]
    decided = client.post(
        f"/v1/approvals/{request['id']}",
        json={key: request[key] for key in ("argument_digest", "policy_version", "expires_at")}
        | {"decision": "approve"},
    )
    assert decided.status_code == 200, decided.text
    run = _run_to_end(client, url)
    assert run["status"] == "SUCCEEDED" and run["result"]["approved"] is True, run
    return {"approval": "approved", "run": run["status"]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--installer", type=Path, required=True)
    parser.add_argument(
        "--python", type=Path, required=True, help="Interpreter with runweave and the sample agent"
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--replace-data", action="store_true", help="Remove existing app data first"
    )
    args = parser.parse_args()
    config, data = app_folders(sys.platform, Path.home(), dict(os.environ))
    if (config.exists() or data.exists()) and not args.replace_data:
        raise SystemExit(
            f"RunWeave already has data here ({config}, {data}); pass --replace-data to remove it"
        )
    for folder in {config, data}:
        shutil.rmtree(folder, ignore_errors=True)
    work = Path(tempfile.mkdtemp(prefix="runweave-desktop-check-"))
    report: dict[str, Any] = {"platform": sys.platform, "installer": args.installer.name}
    installed = install(args.installer, work)
    report["installed"] = str(installed.executable)
    try:
        python = args.python.absolute()  # not resolve(): a venv's python is a symlink
        config.mkdir(parents=True, exist_ok=True)
        choices = config / "desktop.json"
        choices.write_text(
            json.dumps({"project": str(make_project(python, work / "demo")), "python": str(python)})
        )
        report["first_launch"] = session(installed.executable, data, choices, first_use)
        refund = make_project(python, work / "refunds", refund=True)
        choices.write_text(json.dumps({"project": str(refund), "python": str(python)}))
        report["second_launch"] = session(installed.executable, data, choices, approval_use)
    finally:
        report["left_before_uninstall"] = _gone(_app_processes, 60)
        installed.uninstall()
        report["uninstalled"] = _until(installed.removed, "the uninstaller", 180)
        report["left_after_uninstall"] = _app_processes()
        for folder in {config, data}:
            shutil.rmtree(folder, ignore_errors=True)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    for launch in ("first_launch", "second_launch"):
        assert report[launch]["left_running"] == [], report[launch]
        assert report[launch]["postgres_shut_down"], report[launch]
    assert report["left_before_uninstall"] == report["left_after_uninstall"] == [], report
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
