"""The developer docs: references generated from the code, links, and the runnable quickstart."""

import argparse
import importlib.util
import os
import re
import sys
import typing
from pathlib import Path
from types import ModuleType

import yaml
from pydantic import BaseModel

from runweave.cli import parser
from runweave.core.settings import Settings
from runweave.project import (
    AgentSection,
    BenchSection,
    DatasetSection,
    EvalSection,
    LimitsSection,
    ProxySection,
    ShadowSection,
)
from runweave.proxy.faults import BUILT_IN_PROFILES, FaultKind

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"


def _script(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        name.replace("-", "_"), ROOT / "scripts" / f"{name}.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


reference = _script("reference-docs")
check = _script("check-docs")


def _read(name: str) -> str:
    return (DOCS / name).read_text(encoding="utf-8")


# Generated references


def test_the_generated_references_are_current() -> None:
    assert reference.stale(ROOT) == [], "run: uv run python scripts/reference-docs.py"


def test_a_generated_section_is_replaced_between_its_markers() -> None:
    text = "intro\n<!-- generated:x -->\nold\n<!-- /generated:x -->\noutro\n"
    assert reference.render(text, "x", "new\n") == (
        "intro\n<!-- generated:x -->\nnew\n<!-- /generated:x -->\noutro\n"
    )


def _options(root: argparse.ArgumentParser) -> list[str]:
    found = []
    for action in root._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, sub in action.choices.items():
                found.append(f" {name}`")  # a heading such as `runweave approvals list`
                found += _options(sub)
        elif action.help != argparse.SUPPRESS:
            found += [f"`{option}" for option in action.option_strings if option.startswith("--")]
    return found


def test_every_command_and_option_is_in_the_cli_reference() -> None:
    text = _read("cli.md")
    missing = [token for token in _options(parser()) if token not in text]
    assert missing == []


def test_every_setting_is_in_the_configuration_reference() -> None:
    text = _read("configuration.md")
    missing = [name for name in Settings.model_fields if f"RUNWEAVE_{name.upper()}" not in text]
    assert missing == []


def test_every_setting_belongs_to_a_documented_group() -> None:
    assert [name for name in Settings.model_fields if reference.group_of(name) is None] == []


def test_every_project_file_key_is_in_the_configuration_reference() -> None:
    text = _read("configuration.md")
    sections: dict[str, type[BaseModel]] = {
        "agent": AgentSection,
        "limits": LimitsSection,
        "proxy": ProxySection,
        "datasets": DatasetSection,
        "eval": EvalSection,
        "bench": BenchSection,
        "shadow": ShadowSection,
    }
    missing = [
        f"{section}.{key}"
        for section, model in sections.items()
        for key in model.model_fields
        if f"`{key}`" not in text
    ]
    assert missing == []


def test_every_fault_and_built_in_profile_is_in_the_catalog() -> None:
    text = _read("faults.md")
    kinds = typing.get_args(FaultKind)
    assert [kind for kind in kinds if f"`{kind}`" not in text] == []
    assert [name for name in BUILT_IN_PROFILES if f"`{name}`" not in text] == []


# The set of pages


REQUIRED = [
    "quickstart.md",
    "concepts.md",
    "cli.md",
    "configuration.md",
    "http-agents.md",
    "llm-proxy.md",
    "faults.md",
    "workspace.md",
    "reports.md",
    "approvals.md",
    "budgets.md",
    "limits.md",
    "troubleshooting.md",
]


def test_the_index_links_every_page() -> None:
    index = _read("README.md")
    pages = sorted(path.name for path in DOCS.glob("*.md") if path.name != "README.md")
    assert set(REQUIRED) <= set(pages)
    assert [page for page in pages if f"({page})" not in index] == []


def test_the_workspace_guide_has_a_tour_and_budgets_cover_your_own_key() -> None:
    assert re.search(r"^## .*tour", _read("workspace.md"), re.IGNORECASE | re.MULTILINE)
    assert re.search(r"^## Bring your own key", _read("budgets.md"), re.MULTILINE)


def test_the_limits_page_says_a_mock_benchmark_is_not_provider_capacity() -> None:
    assert "not provider capacity" in _read("limits.md")


# Links


def test_every_relative_link_and_anchor_resolves() -> None:
    files = [ROOT / "README.md", *sorted(DOCS.rglob("*.md"))]
    assert check.broken_links(files) == []


def test_broken_links_and_anchors_are_reported(tmp_path: Path) -> None:
    (tmp_path / "b.md").write_text("# Real heading\n", encoding="utf-8")
    page = tmp_path / "a.md"
    page.write_text(
        "[ok](b.md) [ok](b.md#real-heading) [ok](#here) [web](https://example.com)\n"
        "[gone](missing.md) [bad](b.md#nope)\n```\n[code](ignored.md)\n```\n## Here\n",
        encoding="utf-8",
    )
    assert check.broken_links([page]) == [f"{page}: missing.md", f"{page}: b.md#nope"]


def test_links_into_this_repository_on_github_resolve_to_its_files(tmp_path: Path) -> None:
    page = tmp_path / "README.md"
    blob, raw = f"{check.REPOSITORY}/blob/main", check.RAW
    page.write_text(
        f"[ok]({blob}/docs/quickstart.md#1-install-runweave) ![img]({raw}/docs/images/demo.gif)\n"
        f"[dir]({check.REPOSITORY}/tree/main/examples/github-actions)\n"
        f"[gone]({blob}/docs/missing.md) [anchor]({blob}/docs/quickstart.md#nope)\n",
        encoding="utf-8",
    )
    assert check.broken_links([page], root=ROOT) == [
        f"{page}: {blob}/docs/missing.md",
        f"{page}: {blob}/docs/quickstart.md#nope",
    ]


def test_the_readme_renders_on_pypi_with_absolute_links_and_images() -> None:
    # PyPI shows README.md as the long description, where relative links and images break.
    text = check.FENCE.sub("", (ROOT / "README.md").read_text(encoding="utf-8"))
    prose = re.sub(r"`[^`\n]*`", "", text)
    targets = check.LINK.findall(prose) + re.findall(r"(?:src|href)=\"([^\"]+)\"", prose)
    assert targets
    relative = [t for t in targets if not re.match(r"^(https://|mailto:)", t)]
    assert relative == []
    images = re.findall(r"!\[[^\]]*\]\(([^)\s]+)", prose)
    assert images and all(image.startswith(check.RAW + "/") for image in images)


def test_anchors_follow_githubs_heading_slugs() -> None:
    assert (
        check.slug("Test your agent: `runweave init`, `eval`")
        == "test-your-agent-runweave-init-eval"
    )
    assert check.slug("1. Create a project") == "1-create-a-project"
    assert check.slug("Service settings (`RUNWEAVE_*`)") == "service-settings-runweave_"


# The runnable quickstart


SAMPLE = """# Try it

<!-- quickstart:run -->
```bash
git clone https://github.com/iam-mkd/RunWeave.git
cd RunWeave  # into the clone
```

Not this one:

```bash
echo skipped
```

<!-- quickstart:background http://127.0.0.1:8000/ -->
```bash
uv run runweave serve
```
"""


def test_only_marked_blocks_run_in_order() -> None:
    steps = check.quickstart_steps(SAMPLE)
    assert [(step.command, step.background) for step in steps] == [
        ("git clone https://github.com/iam-mkd/RunWeave.git", None),
        ("cd RunWeave", None),
        ("uv run runweave serve", "http://127.0.0.1:8000/"),
    ]


def test_only_the_registry_install_is_replaced_by_the_wheel_under_test(tmp_path: Path) -> None:
    wheel = tmp_path / "runweave-0.1.0-py3-none-any.whl"
    install = "uv tool install runweave"
    assert check.substitute(install, wheel) == f'uv tool install "{wheel}"'
    assert check.substitute(install, None) == install  # after the release: the registry
    for other in ("runweave init my-agent", "uv tool install runweave-other", "runweave eval"):
        assert check.substitute(other, wheel) == other


def test_the_quickstart_tool_folders_stay_inside_its_scratch_folder(tmp_path: Path) -> None:
    environment = check.tool_environment({"PATH": "/usr/bin"}, tmp_path)
    assert environment["UV_TOOL_DIR"] == str(tmp_path / "uv-tools")
    assert environment["UV_TOOL_BIN_DIR"] == str(tmp_path / "uv-bin")
    # As a new terminal after `uv tool update-shell`: the installed command is on PATH.
    assert environment["PATH"].split(os.pathsep)[0] == str(tmp_path / "uv-bin")


def test_the_quickstart_runs_end_to_end_without_a_key() -> None:
    steps = check.quickstart_steps(_read("quickstart.md"))
    commands = [step.command for step in steps]
    # The primary path installs the package: no clone, no Node, no `uv run`.
    assert commands[0] == "uv tool install runweave"
    assert commands[1] == "runweave --version"
    assert not any(c.startswith(("git ", "npm ", "uv run", "uv sync")) for c in commands)
    for expected in ("runweave init", "runweave eval", "runweave bench"):
        assert any(command.startswith(expected) for command in commands), expected
    assert [step for step in steps if step.background], "serve runs in the background"
    # The same lines work in bash, zsh, PowerShell and cmd.
    shell_only = re.compile(r"&&|\|\||\$\w|%\w+%|^export |^set |\\$")
    assert [c for c in commands if shell_only.search(c)] == []
    assert not any(re.search(r"(?i)api[_-]?key", command) for command in commands)


def test_the_quickstart_also_gives_pip_and_the_from_source_path() -> None:
    page = _read("quickstart.md")
    assert "pipx install runweave" in page and "pip install runweave" in page
    assert "uv tool update-shell" in page  # when `runweave` is not found after installing
    source = page[page.index("## From source") :]
    for expected in ("git clone https://github.com/iam-mkd/RunWeave.git", "uv sync",
                     "npm --prefix frontend run build", "uv run runweave"):  # fmt: skip
        assert expected in source, expected


def test_ci_runs_the_quickstart_on_three_fresh_runners_without_secrets() -> None:
    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "docs.yml").read_text("utf-8"))
    job = workflow["jobs"]["quickstart"]
    assert job["strategy"]["matrix"]["os"] == ["ubuntu-latest", "windows-latest", "macos-latest"]
    assert any("check-docs.py --quickstart" in step.get("run", "") for step in job["steps"])
    assert "secrets." not in (ROOT / ".github" / "workflows" / "docs.yml").read_text("utf-8")


def test_until_the_release_exists_ci_installs_the_wheel_it_built() -> None:
    jobs = yaml.safe_load((ROOT / ".github" / "workflows" / "docs.yml").read_text("utf-8"))["jobs"]
    build = "\n".join(step.get("run", "") for step in jobs["wheel"]["steps"])
    for expected in ("npm --prefix frontend run build", "bundle-workspace-ui.py", "uv build"):
        assert expected in build, expected
    quickstart = jobs["quickstart"]
    assert quickstart["needs"] == "wheel"
    run = "\n".join(step.get("run", "") for step in quickstart["steps"])
    assert "--wheel" in run
    # The reader needs only uv: no Node on the quickstart runners.
    assert not any("setup-node" in step.get("uses", "") for step in quickstart["steps"])


def test_the_examples_never_give_a_bare_runweave_command() -> None:
    # The dry run: a bare `runweave eval` on the examples page failed with "command not
    # found", because the examples run RunWeave through uv from a clone.
    pages = [DOCS / "examples.md", *sorted((ROOT / "examples").glob("*/README.md"))]
    bare = [
        f"{page.relative_to(ROOT)}: {line}"
        for page in pages
        for block in re.findall(
            r"```(?:bash|sh|powershell)\n(.*?)```", page.read_text("utf-8"), re.S
        )
        for line in block.splitlines()
        if line.strip().startswith("runweave ")
    ]
    assert bare == []
