"""Release readiness: the workspace UI ships inside the package, and the example CI job
really fails a pull request that regresses the agent."""

import os
import shlex
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import yaml

from runweave.workspace import BUNDLED_UI, workspace_assets

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "github-actions" / "runweave.yml"


def built(folder: Path) -> Path:
    (folder / "assets").mkdir(parents=True)
    (folder / "index.html").write_text("<!doctype html>", encoding="utf-8")
    return folder


# The bundled UI


def test_serve_prefers_explicit_then_bundled_then_checkout_assets(tmp_path: Path) -> None:
    bundled, checkout, explicit = tmp_path / "bundled", tmp_path / "checkout", tmp_path / "mine"
    assert workspace_assets(None, bundled=bundled, checkout=checkout) == checkout.resolve()
    built(checkout)
    assert workspace_assets(None, bundled=bundled, checkout=checkout) == checkout.resolve()
    built(bundled)
    assert workspace_assets(None, bundled=bundled, checkout=checkout) == bundled.resolve()
    assert workspace_assets(explicit, bundled=bundled, checkout=checkout) == explicit.resolve()
    assert BUNDLED_UI.parent == ROOT / "src" / "runweave"


def test_the_bundle_script_copies_the_build_into_the_package(tmp_path: Path) -> None:
    source = built(tmp_path / "dist")
    (source / "assets" / "index-abc.js").write_text("ok", encoding="utf-8")
    target = tmp_path / "package" / "workspace_ui"
    (target / "assets").mkdir(parents=True)
    (target / "assets" / "index-stale.js").write_text("old", encoding="utf-8")
    script = ROOT / "scripts" / "bundle-workspace-ui.py"
    copied = subprocess.run(
        [sys.executable, str(script), "--source", str(source), "--target", str(target)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert copied.returncode == 0, copied.stderr
    assert sorted(p.name for p in (target / "assets").iterdir()) == ["index-abc.js"]
    assert (target / "index.html").is_file()
    missing = subprocess.run(
        [sys.executable, str(script), "--source", str(tmp_path / "nope"), "--target", str(target)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert missing.returncode != 0 and "npm" in missing.stderr


def test_package_data_includes_the_bundled_ui() -> None:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert '"workspace_ui/**/*"' in text
    assert "src/runweave/workspace_ui/" in (ROOT / ".gitignore").read_text(encoding="utf-8")


# The example GitHub Actions job


def runweave_steps() -> list[str]:
    workflow: dict[str, Any] = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    (job,) = workflow["jobs"].values()
    return [step["run"] for step in job["steps"] if "run" in step and "runweave" in step["run"]]


def run_step(command: str, project: Path) -> subprocess.CompletedProcess[str]:
    words = shlex.split(command)
    assert words[:2] == ["uvx", "runweave"], command
    env = {k: v for k, v in os.environ.items() if not k.startswith("RUNWEAVE_")}
    env["PYTHONPATH"] = str(ROOT / "src")
    return subprocess.run(
        [sys.executable, "-m", "runweave.cli", *words[2:]],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        timeout=240,
        check=False,
    )


def test_the_example_ci_job_passes_and_then_fails_on_a_seeded_regression(tmp_path: Path) -> None:
    steps = runweave_steps()
    assert any(" eval" in step for step in steps) and any(" bench" in step for step in steps)
    assert all("--server" not in step for step in steps)  # CI needs nothing else running

    init = run_step("uvx runweave init proj", tmp_path)
    assert init.returncode == 0, init.stderr
    project = tmp_path / "proj"
    toml = project / "runweave.toml"
    toml.write_text(
        toml.read_text(encoding="utf-8").replace(
            '# baseline = ".runweave/baselines/smoke.json"',
            'baseline = ".runweave/baselines/smoke.json"',
        ),
        encoding="utf-8",
    )
    saved = run_step("uvx runweave eval --save-baseline", project)
    assert saved.returncode == 0, saved.stdout + saved.stderr

    for step in steps:  # a healthy pull request passes every step
        result = run_step(step, project)
        assert result.returncode == 0, (step, result.stdout + result.stderr)

    agent = project / "agent.py"
    agent.write_text(
        agent.read_text(encoding="utf-8").replace("longest=longest", 'longest="regressed"'),
        encoding="utf-8",
    )
    (eval_step,) = [step for step in steps if " eval" in step]
    failed = run_step(eval_step, project)
    assert failed.returncode == 1, failed.stdout + failed.stderr
    assert "regress" in failed.stdout.lower()
    junit = next(word for word in shlex.split(eval_step) if word.endswith(".xml"))
    assert ET.parse(project / junit).getroot().attrib["failures"] != "0"
