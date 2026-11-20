"""`runweave init` and `runweave eval` end to end, offline, in subprocesses like a developer."""

import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
REQUIRE_ALL = os.environ.get("RUNWEAVE_CONTRACT_REQUIRE_ALL") == "1"


def runweave(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("RUNWEAVE_")}
    env["PYTHONPATH"] = str(ROOT / "src")
    return subprocess.run(
        [sys.executable, "-m", "runweave.cli", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )


def report(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((path / "report.json").read_text(encoding="utf-8"))
    return data


def latest(project: Path) -> Path:
    # By created_at: folder names stamp only the second, then a random suffix, so two runs
    # in the same second would sort by chance.
    runs = sorted(
        (project / ".runweave" / "reports").glob("eval-*"),
        key=lambda run: report(run)["created_at"],
    )
    assert runs, "no report written"
    return runs[-1]


def test_init_creates_a_python_project_that_passes_its_own_eval(tmp_path: Path) -> None:
    created = runweave("init", "my-agent", cwd=tmp_path)
    assert created.returncode == 0, created.stderr
    project = tmp_path / "my-agent"
    assert {p.name for p in project.iterdir()} >= {
        "runweave.toml",
        "agent.py",
        "datasets",
        "README.md",
    }
    again = runweave("init", "my-agent", cwd=tmp_path)
    assert again.returncode != 0 and "--force" in again.stderr

    evaluated = runweave("eval", cwd=project)
    assert evaluated.returncode == 0, evaluated.stdout + evaluated.stderr
    assert "passed" in evaluated.stdout.lower()
    folder = latest(project)
    data = report(folder)
    assert data["kind"] == "eval" and data["summary"]["failed"] == 0
    assert data["summary"]["total"] >= 3 and data["agent"]["name"] == "word-count"
    assert data["environment"]["mode"] == "in-process"
    assert {"python", "platform", "runweave"} <= set(data["environment"])
    junit = ET.parse(folder / "junit.xml").getroot()
    assert junit.tag == "testsuites" and junit.attrib["failures"] == "0"
    assert "word-count" in (folder / "report.md").read_text(encoding="utf-8")


def test_failing_cases_exit_non_zero_and_are_reported(tmp_path: Path) -> None:
    runweave("init", "proj", cwd=tmp_path)
    project = tmp_path / "proj"
    dataset = project / "datasets" / "smoke.jsonl"
    with dataset.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {"id": "wrong", "input": {"text": "one two"}, "expect": {"output": {"words": 3}}}
            )
            + "\n"
        )
    evaluated = runweave("eval", "--junit", "out/junit.xml", cwd=project)
    assert evaluated.returncode == 1
    assert "wrong" in evaluated.stdout
    data = report(latest(project))
    failed = [c for c in data["cases"] if not c["passed"]]
    assert [c["id"] for c in failed] == ["wrong"]
    assert any(a["name"] == "output" and not a["passed"] for a in failed[0]["assertions"])
    junit = ET.parse(project / "out" / "junit.xml").getroot()
    assert junit.attrib["failures"] == "1"


def test_a_baseline_catches_regressions_even_above_the_pass_rate(tmp_path: Path) -> None:
    runweave("init", "proj", cwd=tmp_path)
    project = tmp_path / "proj"
    first = runweave("eval", "--save-baseline", cwd=project)
    assert first.returncode == 0, first.stderr
    baseline = project / ".runweave" / "baselines" / "smoke.json"
    assert baseline.is_file()
    (project / "runweave.toml").write_text(
        (project / "runweave.toml")
        .read_text(encoding="utf-8")
        .replace("min_pass_rate = 1.0", "min_pass_rate = 0.5")
        + "\n",
        encoding="utf-8",
    )
    agent = project / "agent.py"
    agent.write_text(
        agent.read_text(encoding="utf-8").replace("longest=longest", 'longest="regressed"'),
        encoding="utf-8",
    )
    second = runweave("eval", "--baseline", str(baseline), cwd=project)
    assert second.returncode == 1, second.stdout
    assert "regress" in second.stdout.lower()
    data = report(latest(project))
    assert data["baseline"]["regressions"]


def test_config_errors_exit_2_with_a_clear_message(tmp_path: Path) -> None:
    missing = runweave("eval", cwd=tmp_path)
    assert missing.returncode == 2 and "runweave init" in missing.stderr
    assert "Traceback" not in missing.stderr


@pytest.mark.asyncio
async def test_eval_can_submit_to_a_running_server(tmp_path: Path) -> None:
    runweave("init", "proj", cwd=tmp_path)
    project = tmp_path / "proj"
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    env = {k: v for k, v in os.environ.items() if not k.startswith("RUNWEAVE_")}
    env["PYTHONPATH"] = os.pathsep.join([str(ROOT / "src"), str(project)])
    server = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "runweave.cli",
        "serve",
        "--demo",
        "--api-only",
        "--port",
        str(port),
        "--agent",
        "agent:run",
        cwd=project,
        env=env,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        import httpx

        async with httpx.AsyncClient() as client:
            for _ in range(200):
                try:
                    if (
                        await client.get(f"http://127.0.0.1:{port}/health/ready")
                    ).status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(0.1)
        evaluated = await asyncio.to_thread(
            runweave, "eval", "--server", f"http://127.0.0.1:{port}", cwd=project
        )
    finally:
        server.kill()
        await server.wait()
    assert evaluated.returncode == 0, evaluated.stdout + evaluated.stderr
    assert report(latest(project))["environment"]["mode"] == f"server http://127.0.0.1:{port}"


def test_init_creates_a_typescript_project_that_passes_its_own_eval(tmp_path: Path) -> None:
    node, npm = shutil.which("node"), shutil.which("npm")
    if not (node and npm):
        if REQUIRE_ALL:
            pytest.fail("Node.js is required for the TypeScript project test")
        pytest.skip("Node.js is not installed")
    created = runweave("init", "ts-agent", "--language", "typescript", cwd=tmp_path)
    assert created.returncode == 0, created.stderr
    project = tmp_path / "ts-agent"
    assert (project / "vendor" / "runweave-agent" / "dist" / "index.js").is_file()
    manifest = json.loads((project / "package.json").read_text(encoding="utf-8"))
    assert manifest["dependencies"]["@runweave/agent"] == "file:vendor/runweave-agent"
    installed = subprocess.run(
        [npm, "install", "--no-audit", "--no-fund"],
        cwd=project,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
        shell=sys.platform == "win32",
    )
    assert installed.returncode == 0, installed.stderr
    evaluated = runweave("eval", cwd=project)
    assert evaluated.returncode == 0, evaluated.stdout + evaluated.stderr
    assert report(latest(project))["agent"]["kind"] == "http"


def test_the_vendored_typescript_sdk_matches_the_built_sdk() -> None:
    built = ROOT / "sdk" / "typescript" / "dist"
    vendored = ROOT / "src" / "runweave" / "templates" / "typescript" / "vendor" / "runweave-agent"
    if not built.is_dir():
        if REQUIRE_ALL:
            pytest.fail("Build sdk/typescript first")
        pytest.skip("sdk/typescript is not built")
    names = sorted(p.name for p in built.glob("*.js"))
    assert names == sorted(p.name for p in (vendored / "dist").glob("*.js"))
    for name in names:
        # Regenerate with: python scripts/vendor-typescript-sdk.py
        assert (vendored / "dist" / name).read_bytes().replace(b"\r\n", b"\n") == (
            built / name
        ).read_bytes().replace(b"\r\n", b"\n"), name
