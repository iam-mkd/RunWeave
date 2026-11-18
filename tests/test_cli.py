import os
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from runweave.core.settings import Settings

ROOT = Path(__file__).resolve().parents[1]


def run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("RUNWEAVE_")}
    env["PYTHONPATH"] = os.pathsep.join(
        [str(ROOT / "src"), str(ROOT / "examples" / "sample-agent")]
    )
    return subprocess.run(
        [sys.executable, "-m", "runweave.cli", *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )


def test_serve_check_loads_agents_and_exits() -> None:
    result = run_cli("serve", "--demo", "--check", "--agent", "sample_agent:text_report")
    assert result.returncode == 0, result.stderr
    assert "incident-triage" in result.stdout
    assert "text-report 1.0.0 (python: sample_agent:text_report)" in result.stdout


def test_serve_reports_bad_agent_targets_without_a_traceback() -> None:
    result = run_cli("serve", "--demo", "--check", "--agent", "missing_module:run")
    assert result.returncode != 0
    assert "could not be imported" in result.stderr
    assert "Traceback" not in result.stderr


def test_serve_rejects_non_loopback_hosts() -> None:
    result = run_cli("serve", "--demo", "--check", "--host", "0.0.0.0")
    assert result.returncode != 0
    assert "loopback" in result.stderr


@pytest.mark.parametrize("target", ["not a target", "pkg:attr:extra", "../evil:run"])
def test_settings_validate_agent_targets(target: str) -> None:
    with pytest.raises(ValidationError):
        Settings(agents=(target,))
