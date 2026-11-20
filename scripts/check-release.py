"""Clean-machine release check: build a wheel and use it the way a new developer would.

    npm --prefix frontend run build
    uv run --no-sync python scripts/check-release.py --output target/release/check.json
    python scripts/check-release.py --wheel dist/runweave-0.1.0-py3-none-any.whl --installer pip

Outside the repository, with no RunWeave settings or provider keys in the environment,
it installs the wheel into a fresh environment (`uvx --from <wheel>` by default; a new
virtual environment with `pip`, or `uv tool install` with its folders in the scratch
folder) and uses that `runweave` to create Python and TypeScript projects, then eval,
bench and shadow from the CLI, then serves the workspace and runs an eval and a load test
through the same jobs API the screens use. Offline apart from installing the wheel's
dependencies; no API key is needed or used. Needs only httpx besides the standard library.
"""

import argparse
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
SECRET_PREFIXES = ("RUNWEAVE_", "OPENAI_", "ANTHROPIC_", "GROQ_", "XAI_", "GEMINI_")


INSTALLERS = ("uvx", "pip", "uv-tool")


class CheckFailed(RuntimeError):
    pass


@dataclass(frozen=True)
class Install:
    """How to put the wheel in a fresh environment, and the `runweave` it then gives."""

    steps: list[list[str]]
    command: list[str]
    env: dict[str, str] = field(default_factory=dict)


def plan_install(installer: str, wheel: Path, scratch: Path) -> Install:
    scripts, exe = ("Scripts", ".exe") if sys.platform == "win32" else ("bin", "")
    if installer == "uvx":
        return Install([], ["uvx", "--from", str(wheel), "runweave"])
    if installer == "pip":
        venv = scratch / "venv"
        python = str(venv / scripts / f"python{exe}")
        return Install(
            [
                [sys.executable, "-m", "venv", str(venv)],
                [python, "-m", "pip", "install", str(wheel)],
            ],  # fmt: skip
            [str(venv / scripts / f"runweave{exe}")],
        )
    if installer == "uv-tool":
        tools, bin_dir = scratch / "uv-tools", scratch / "uv-bin"
        return Install(
            [["uv", "tool", "install", str(wheel)]],
            [str(bin_dir / f"runweave{exe}")],
            {"UV_TOOL_DIR": str(tools), "UV_TOOL_BIN_DIR": str(bin_dir)},
        )
    raise ValueError(f"Unknown installer {installer!r}; choose one of {INSTALLERS}")


def clean_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(SECRET_PREFIXES)}
    env.pop("PYTHONPATH", None)
    env.pop("VIRTUAL_ENV", None)
    return env


def run(args: list[str], cwd: Path, env: dict[str, str], timeout: float = 600) -> str:
    started = time.perf_counter()
    result = subprocess.run(
        args, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout, check=False
    )
    took = time.perf_counter() - started
    print(f"  [{result.returncode}] {' '.join(args[-6:])} ({took:.1f} s)", flush=True)
    if result.returncode != 0:
        raise CheckFailed(
            f"{' '.join(args)} exited {result.returncode}:\n{result.stdout}\n{result.stderr}"
        )
    return result.stdout


def build_wheel(out: Path) -> Path:
    if not (ROOT / "frontend" / "dist" / "index.html").is_file():
        raise CheckFailed("Build the workspace first: npm --prefix frontend run build")
    subprocess.run([sys.executable, str(ROOT / "scripts" / "bundle-workspace-ui.py")], check=True)
    shutil.rmtree(out, ignore_errors=True)
    subprocess.run(["uv", "build", "--wheel", "--out-dir", str(out)], cwd=ROOT, check=True)
    (wheel,) = out.glob("runweave-*.whl")
    return wheel


def latest(project: Path, kind: str) -> dict[str, Any]:
    folder = sorted((project / ".runweave" / "reports").glob(f"{kind}-*"))[-1]
    data: dict[str, Any] = json.loads((folder / "report.json").read_text(encoding="utf-8"))
    return data


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


def wait_job(client: httpx.Client, job_id: str, seconds: float = 300) -> dict[str, Any]:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        body: dict[str, Any] = client.get(f"/v1/jobs/{job_id}").json()
        if body["job"]["status"] != "RUNNING":
            return body["job"]  # type: ignore[no-any-return]
        time.sleep(0.5)
    raise CheckFailed(f"Job {job_id} did not finish in {seconds} s")


def check_ui(runweave: list[str], project: Path, env: dict[str, str]) -> dict[str, Any]:
    port = free_port()
    log = (project / "serve.log").open("w", encoding="utf-8")
    server = subprocess.Popen(
        [*runweave, "serve", "--port", str(port), "--agent", "agent:run"],
        cwd=project,
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=sys.platform != "win32",
    )
    try:
        with httpx.Client(
            base_url=f"http://127.0.0.1:{port}", timeout=30, trust_env=False
        ) as client:
            deadline = time.monotonic() + 120
            while True:
                if server.poll() is not None:
                    log.flush()
                    output = (project / "serve.log").read_text(encoding="utf-8")
                    raise CheckFailed(f"runweave serve exited: {output[-2000:]}")
                try:
                    if client.get("/health/ready").status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                if time.monotonic() > deadline:
                    raise CheckFailed("runweave serve did not become ready")
                time.sleep(0.3)
            page = client.get("/")
            if page.status_code != 200 or 'id="root"' not in page.text:
                raise CheckFailed("The packaged workspace UI was not served")
            script = page.text.split('src="', 1)[1].split('"', 1)[0]
            if client.get(script).status_code != 200:
                raise CheckFailed(f"The packaged UI asset {script} was not served")
            agents = [a["name"] for a in client.get("/v1/agents").json()["agents"]]
            project_info = client.get("/v1/jobs/project").json()
            results = {}
            for request in (
                {"kind": "eval"},
                {
                    "kind": "bench",
                    "concurrency": [1, 4],
                    "runs": 8,
                    "fault_profiles": ["", "flaky"],
                },
            ):
                started = client.post("/v1/jobs", json=request)
                if started.status_code != 202:
                    raise CheckFailed(f"Starting {request} failed: {started.text}")
                job = wait_job(client, started.json()["id"])
                if job["status"] != "COMPLETED" or not job["passed"]:
                    raise CheckFailed(f"UI job {request['kind']} ended {job}")
                results[request["kind"]] = job["report_id"]
            listed = client.get("/v1/reports").json()["reports"]
            return {
                "ui_served": True,
                "agents": agents,
                "project_agent": project_info["agent"],
                "jobs": results,
                "reports_listed": len(listed),
            }
    finally:
        stop_tree(server)
        log.close()


def stop_tree(process: "subprocess.Popen[str]") -> None:
    """Stop uvx and the server it started (on Windows, killing uvx alone orphans it)."""
    if process.poll() is not None:
        return
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(process.pid)], capture_output=True, check=False
        )
    else:
        import signal

        os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=20)
    except subprocess.TimeoutExpired:
        process.kill()


def main() -> int:
    parser = argparse.ArgumentParser(description="Clean-machine RunWeave release check")
    parser.add_argument("--wheel", type=Path, help="Use this wheel instead of building one")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--skip-typescript", action="store_true")
    parser.add_argument(
        "--installer", choices=INSTALLERS, default="uvx", help="How to install the wheel"
    )
    args = parser.parse_args()
    report: dict[str, Any] = {
        "platform": platform.platform(),
        "python": platform.python_version(),
        "installer": args.installer,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    try:
        wheel = (args.wheel or build_wheel(ROOT / "target" / "release" / "dist")).resolve()
        report["wheel"] = wheel.name
        with tempfile.TemporaryDirectory(
            prefix="runweave-clean-", ignore_cleanup_errors=True
        ) as scratch:
            work = Path(scratch)
            install = plan_install(args.installer, wheel, work / ".install")
            env = {**clean_env(), **install.env}
            print(f"Install the wheel ({args.installer})", flush=True)
            for step in install.steps:
                run(step, work, env)
            runweave = install.command
            report["version"] = run([*runweave, "--version"], work, env).strip()
            if report["version"] != f"runweave {wheel.name.split('-')[1]}":
                raise CheckFailed(f"{report['version']} is not the wheel's version")
            print("Python project from the CLI", flush=True)
            run([*runweave, "init", "py-agent"], work, env)
            py = work / "py-agent"
            run([*runweave, "eval"], py, env)
            run([*runweave, "bench", "--concurrency", "1", "4", "--runs", "8"], py, env)
            run([*runweave, "shadow", "--candidate", "agent:run"], py, env)
            report["python_project"] = {
                "eval_passed": latest(py, "eval")["gates"]["passed"],
                "bench_levels": len(latest(py, "bench")["cases"]),
                "bench_failures": sum(
                    sum(c["failures"].values()) for c in latest(py, "bench")["cases"]
                ),
                "shadow_eligible": latest(py, "shadow")["promotion_eligible"],
            }
            node, npm = shutil.which("node"), shutil.which("npm")
            if args.skip_typescript or not (node and npm):
                report["typescript_project"] = "skipped (Node.js not found)"
            else:
                print("TypeScript project from the CLI", flush=True)
                run([*runweave, "init", "ts-agent", "--language", "typescript"], work, env)
                ts = work / "ts-agent"
                run([npm, "install", "--no-audit", "--no-fund"], ts, env)
                run([*runweave, "eval"], ts, env)
                run([*runweave, "bench", "--concurrency", "1", "4", "--runs", "8"], ts, env)
                report["typescript_project"] = {
                    "eval_passed": latest(ts, "eval")["gates"]["passed"],
                    "agent_kind": latest(ts, "eval")["agent"]["kind"],
                    "bench_levels": len(latest(ts, "bench")["cases"]),
                }
            print("Workspace UI and jobs API from the wheel", flush=True)
            report["workspace"] = check_ui(runweave, py, env)
        report["passed"] = True
    except (CheckFailed, subprocess.CalledProcessError) as error:
        report["passed"] = False
        report["error"] = str(error)[-4000:]
    rendered = json.dumps(report, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
