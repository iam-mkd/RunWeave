"""The framework examples in examples/: each is a self-contained project that passes offline.

These tests check each example's shape; .github/workflows/examples.yml installs each one and
runs `runweave eval` and `runweave bench` against the mock proxy with outbound traffic blocked.
"""

import json
import re
import tomllib
from pathlib import Path

import pytest

from runweave.evals.cases import load_cases
from runweave.project import load_project
from runweave.proxy.mock import load_fixtures

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"
PYTHON_EXAMPLES = ("plain-python", "openai-agents", "claude-agent-sdk", "langgraph")
NODE_EXAMPLES = ("typescript",)
ALL_EXAMPLES = PYTHON_EXAMPLES + NODE_EXAMPLES
EXACT_PYTHON_PIN = re.compile(r"^[A-Za-z0-9][\w.-]*(\[[\w,.-]+\])?==[0-9][\w.+!-]*$")
EXACT_NPM_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?$")


def _root_version() -> str:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return str(data["project"]["version"])


@pytest.mark.parametrize("name", ALL_EXAMPLES)
def test_example_is_a_project_in_mock_mode_with_fixtures(name: str) -> None:
    folder = EXAMPLES / name
    project = load_project(folder / "runweave.toml")

    assert project.root == folder
    assert project.proxy.mode == "mock"
    assert project.mock_fixtures == folder / "fixtures.json"
    assert (folder / "README.md").is_file()
    load_fixtures(project.mock_fixtures)  # raises if the proxy would refuse them at startup


@pytest.mark.parametrize("name", ALL_EXAMPLES)
def test_example_dataset_covers_a_tool_call_an_answer_and_a_rejection(name: str) -> None:
    folder = EXAMPLES / name
    project = load_project(folder / "runweave.toml")
    cases = [case for dataset in project.datasets for case in load_cases(dataset.path)]
    fixtures = load_fixtures(project.mock_fixtures)
    scripted_tools = {call.name for rule in fixtures.rules for call in rule.reply.tool_calls or ()}

    tools_expected = {tool for case in cases for tool in case.expect.tool_called or ()}
    # MCP tools reach the model as mcp__<server>__<tool>; the timeline step names the tool.
    unscripted = [
        tool
        for tool in tools_expected
        if not any(name == tool or name.endswith(f"__{tool}") for name in scripted_tools)
    ]
    assert tools_expected and not unscripted
    assert any(
        case.expect.output or case.expect.contains or case.expect.matches for case in cases
    ), "a case checks the final answer"
    assert any(case.expect.status == "REJECTED" for case in cases)
    assert any(rule.when.last_role == "tool" and rule.reply.text for rule in fixtures.rules)


@pytest.mark.parametrize("name", PYTHON_EXAMPLES)
def test_python_example_pins_every_dependency_exactly(name: str) -> None:
    data = tomllib.loads((EXAMPLES / name / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies: list[str] = data["project"]["dependencies"]

    loose = [d for d in dependencies if not EXACT_PYTHON_PIN.fullmatch(d.replace(" ", ""))]
    assert dependencies and not loose
    assert f"runweave=={_root_version()}" in dependencies


@pytest.mark.parametrize("name", NODE_EXAMPLES)
def test_node_example_pins_every_dependency_exactly_with_a_lockfile(name: str) -> None:
    folder = EXAMPLES / name
    package = json.loads((folder / "package.json").read_text(encoding="utf-8"))
    lock = json.loads((folder / "package-lock.json").read_text(encoding="utf-8"))
    declared = {**package.get("dependencies", {}), **package.get("devDependencies", {})}

    assert declared
    for dependency, spec in declared.items():
        if spec.startswith("file:"):
            target = (folder / spec.removeprefix("file:")).resolve()
            assert ROOT in target.parents, f"{dependency} must point inside the repository"
        else:
            assert EXACT_NPM_VERSION.fullmatch(spec), f"{dependency} is not pinned: {spec}"
    assert lock["lockfileVersion"] >= 3
    assert lock["packages"][""]["dependencies"] == package.get("dependencies", {})


def test_the_examples_workflow_runs_every_example() -> None:
    workflow = (ROOT / ".github" / "workflows" / "examples.yml").read_text(encoding="utf-8")

    for name in ALL_EXAMPLES:
        assert f"examples/{name}" in workflow or re.search(rf"^\s+- {name}$", workflow, re.M)
    assert "block-egress.sh" in workflow
    blocker = (ROOT / "scripts" / "block-egress.sh").read_text(encoding="utf-8")
    assert all(name in blocker for name in ("HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY"))


def test_the_typescript_example_supersedes_http_agent_typescript() -> None:
    assert not (EXAMPLES / "http-agent-typescript").exists()
    for path in (ROOT / "docs" / "http-agents.md", ROOT / "README.md"):
        assert "http-agent-typescript" not in path.read_text(encoding="utf-8")


def test_examples_doc_lists_every_example_and_the_readme_links_it() -> None:
    doc = (ROOT / "docs" / "examples.md").read_text(encoding="utf-8")

    for name in ALL_EXAMPLES:
        assert f"examples/{name}" in doc
    assert "docs/examples.md" in (ROOT / "README.md").read_text(encoding="utf-8")
