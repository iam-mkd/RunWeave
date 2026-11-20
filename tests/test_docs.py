"""The developer docs: references generated from the code, links, and the runnable quickstart."""

import argparse
import importlib.util
import re
import sys
import typing
from pathlib import Path
from types import ModuleType

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
    sections = {
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


def test_the_clone_is_replaced_by_the_checkout_under_test(tmp_path: Path) -> None:
    command = "git clone https://github.com/iam-mkd/RunWeave.git"
    assert check.substitute(command, tmp_path) == f'git clone "{tmp_path}" RunWeave'
    assert check.substitute("uv sync", tmp_path) == "uv sync"


def test_the_quickstart_runs_end_to_end_without_a_key() -> None:
    steps = check.quickstart_steps(_read("quickstart.md"))
    commands = [step.command for step in steps]
    assert commands[0].startswith("git clone https://github.com/iam-mkd/RunWeave")
    for expected in ("uv sync", "runweave init", "runweave eval", "runweave bench"):
        assert any(expected in command for command in commands), expected
    assert [step for step in steps if step.background], "serve runs in the background"
    # The same lines work in bash, zsh, PowerShell and cmd.
    shell_only = re.compile(r"&&|\|\||\$\w|%\w+%|^export |^set |\\$")
    assert [c for c in commands if shell_only.search(c)] == []
    assert not any(re.search(r"(?i)api[_-]?key", command) for command in commands)
