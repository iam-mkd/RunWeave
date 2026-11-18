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


def test_serve_check_reports_unreachable_http_agents_without_a_traceback() -> None:
    result = run_cli("serve", "--demo", "--check", "--http-agent", "http://127.0.0.1:1")
    assert result.returncode != 0
    assert "AGENT_UNREACHABLE" in result.stderr
    assert "Traceback" not in result.stderr


def test_serve_rejects_non_loopback_http_agents() -> None:
    result = run_cli("serve", "--demo", "--check", "--http-agent", "http://example.com:9100")
    assert result.returncode != 0
    assert "loopback" in result.stderr


def test_serve_check_lists_http_agents_from_a_running_agent_server() -> None:
    env = {key: value for key, value in os.environ.items() if not key.startswith("RUNWEAVE_")}
    env["PYTHONPATH"] = os.pathsep.join(
        [str(ROOT / "src"), str(ROOT / "examples" / "sample-agent")]
    )
    server = subprocess.Popen(
        [sys.executable, "-m", "runweave.cli", "serve-agent", "sample_agent:text_report",
         "--port", "0"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, env=env,
    )
    try:
        assert server.stdout is not None
        line = server.stdout.readline()
        assert line.startswith("RUNWEAVE_AGENT_READY "), line
        url = line.split()[1]
        assert url.startswith("http://127.0.0.1:")
        result = run_cli("serve", "--demo", "--check", "--http-agent", url)
    finally:
        server.kill()
        server.wait(timeout=10)
    assert result.returncode == 0, result.stderr
    assert f"text-report 1.0.0 (http: {url})" in result.stdout


def test_serve_agent_binds_only_to_loopback() -> None:
    result = run_cli("serve-agent", "sample_agent:text_report", "--host", "0.0.0.0")
    assert result.returncode != 0
    assert "loopback" in result.stderr


@pytest.mark.parametrize("url", ["ftp://127.0.0.1", "http://user:pw@127.0.0.1:9"])
def test_settings_validate_http_agent_urls(url: str) -> None:
    with pytest.raises(ValidationError):
        Settings(http_agents=(url,))
