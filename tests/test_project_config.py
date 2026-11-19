"""runweave.toml: the project file that names the agent, limits, proxy settings and datasets."""

from pathlib import Path

import pytest

from runweave.project import ProjectError, load_project

PYTHON = """
[agent]
name = "word-count"
python = "agent:run"

[limits]
run_timeout_seconds = 12
concurrency = 3

[proxy]
mode = "mock"
fault_profile = "flaky"
fault_seed = 9

[[datasets]]
name = "smoke"
path = "datasets/smoke.jsonl"

[eval]
min_pass_rate = 0.8
baseline = "baselines/smoke.json"
"""


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "runweave.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_a_python_project_loads_with_paths_relative_to_the_file(tmp_path: Path) -> None:
    project = load_project(write(tmp_path, PYTHON))
    assert project.root == tmp_path
    assert (project.agent.name, project.agent.python) == ("word-count", "agent:run")
    assert project.limits.run_timeout_seconds == 12 and project.limits.concurrency == 3
    assert (project.proxy.fault_profile, project.proxy.fault_seed) == ("flaky", 9)
    assert project.dataset("smoke").path == tmp_path / "datasets" / "smoke.jsonl"
    assert project.eval.min_pass_rate == 0.8
    assert project.eval.baseline == tmp_path / "baselines" / "smoke.json"
    assert project.eval.reports == tmp_path / ".runweave" / "reports"


def test_defaults_and_http_agents_with_a_start_command(tmp_path: Path) -> None:
    project = load_project(
        write(
            tmp_path,
            """
[agent]
name = "word-count"
command = ["node", "agent.mjs"]

[[datasets]]
name = "smoke"
path = "smoke.yaml"
""",
        )
    )
    assert project.agent.command == ("node", "agent.mjs") and project.agent.python is None
    assert project.limits.concurrency == 4 and project.proxy.mode == "mock"
    assert project.eval.min_pass_rate == 1.0 and project.eval.baseline is None


def test_the_project_file_is_found_from_a_subdirectory(tmp_path: Path) -> None:
    write(tmp_path, PYTHON)
    nested = tmp_path / "src" / "deep"
    nested.mkdir(parents=True)
    assert load_project(None, start=nested).root == tmp_path


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("[agent\nname=1", "not valid TOML"),
        ('[agent]\nname = "x"\n[[datasets]]\nname="d"\npath="d.jsonl"', "python, http or command"),
        (
            '[agent]\nname = "x"\npython = "a:b"\nhttp = "http://127.0.0.1:1"\n'
            '[[datasets]]\nname="d"\npath="d.jsonl"',
            "either python or http",
        ),
        (
            '[agent]\nname = "Bad Name"\npython = "a:b"\n[[datasets]]\nname="d"\npath="d.jsonl"',
            "agent.name",
        ),
        ('[agent]\nname = "x"\npython = "a:b"', "at least one dataset"),
        (
            '[agent]\nname = "x"\npython = "a:b"\n[[datasets]]\nname="d"\npath="../../etc/passwd"',
            "inside the project",
        ),
        (
            '[agent]\nname = "x"\npython = "a:b"\nsurprise = 1\n'
            '[[datasets]]\nname="d"\npath="d.jsonl"',
            "surprise",
        ),
        (
            '[agent]\nname = "x"\npython = "a:b"\n[proxy]\nmode = "live"\n'
            '[[datasets]]\nname="d"\npath="d.jsonl"',
            "proxy.mode",
        ),
    ],
)
def test_invalid_projects_explain_what_to_fix(tmp_path: Path, text: str, message: str) -> None:
    with pytest.raises(ProjectError, match=message):
        load_project(write(tmp_path, text))


def test_a_missing_project_file_says_how_to_create_one(tmp_path: Path) -> None:
    with pytest.raises(ProjectError, match="runweave init"):
        load_project(None, start=tmp_path)


def test_unknown_datasets_are_named(tmp_path: Path) -> None:
    project = load_project(write(tmp_path, PYTHON))
    with pytest.raises(ProjectError, match="smoke"):
        project.dataset("nope")
