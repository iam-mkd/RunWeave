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

from runweave.workspace import BUNDLED_UI, CHECKOUT_UI, workspace_assets

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


def test_a_source_checkout_serves_its_own_build_from_any_folder() -> None:
    # The quickstart serves from a project folder inside the clone, not from its root.
    assert CHECKOUT_UI == ROOT / "frontend" / "dist"


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


def test_serve_imports_project_agents_like_the_installed_command(tmp_path: Path) -> None:
    # The `runweave` console script does not put the working folder on sys.path (unlike
    # `python -m`); `python -P` reproduces that. The template README says to run this.
    assert run_step("uvx runweave init proj", tmp_path).returncode == 0
    env = {k: v for k, v in os.environ.items() if not k.startswith("RUNWEAVE_")}
    env["PYTHONPATH"] = str(ROOT / "src")
    checked = subprocess.run(
        [sys.executable, "-P", "-m", "runweave.cli", "serve", "--check", "--agent", "agent:run"],
        cwd=tmp_path / "proj",
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert checked.returncode == 0, checked.stderr
    assert "word-count 0.1.0" in checked.stdout


# Installing the wheel the ways the quickstart gives


def _check_release() -> Any:
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "check_release", ROOT / "scripts" / "check-release.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_each_installer_puts_the_wheel_in_a_fresh_environment(tmp_path: Path) -> None:
    check = _check_release()
    wheel = tmp_path / "runweave-0.1.0-py3-none-any.whl"
    scripts = "Scripts" if sys.platform == "win32" else "bin"
    exe = ".exe" if sys.platform == "win32" else ""

    uvx = check.plan_install("uvx", wheel, tmp_path / "w")
    assert uvx.steps == [] and uvx.command == ["uvx", "--from", str(wheel), "runweave"]

    pip = check.plan_install("pip", wheel, tmp_path / "w")
    venv = tmp_path / "w" / "venv"
    assert pip.steps[0] == [sys.executable, "-m", "venv", str(venv)]
    assert pip.steps[1] == [str(venv / scripts / f"python{exe}"), "-m", "pip", "install",
                            str(wheel)]  # fmt: skip
    assert pip.command == [str(venv / scripts / f"runweave{exe}")]

    tool = check.plan_install("uv-tool", wheel, tmp_path / "w")
    assert tool.steps == [["uv", "tool", "install", str(wheel)]]
    # Kept inside the scratch folder, so the check never touches the user's own uv tools.
    assert tool.env == {
        "UV_TOOL_DIR": str(tmp_path / "w" / "uv-tools"),
        "UV_TOOL_BIN_DIR": str(tmp_path / "w" / "uv-bin"),
    }
    assert tool.command == [str(tmp_path / "w" / "uv-bin" / f"runweave{exe}")]
