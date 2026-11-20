"""Write the generated parts of the CLI, configuration and fault references from the code.

    uv run python scripts/reference-docs.py           # rewrite the generated sections
    uv run python scripts/reference-docs.py --check   # exit 1 if any is out of date

Each generated section sits between `<!-- generated:NAME -->` and `<!-- /generated:NAME -->`
in a page under docs/; everything around the markers is written by hand.
"""

import argparse
import re
import sys
import types
import typing
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any

import annotated_types
from pydantic import BaseModel, SecretStr
from pydantic.fields import FieldInfo
from pydantic_core import PydanticUndefined

ROOT = Path(__file__).resolve().parents[1]

# Settings groups, matched in order: a name ending in "_" is a prefix, anything else is exact.
GROUPS: list[tuple[str, tuple[str, ...], str]] = [
    ("General", ("app_name",), "The service's display name."),
    (
        "Authentication and tenants",
        ("auth_", "tenant_", "protected_values"),
        "Who may call the API and how much each tenant may queue. See [Authentication and "
        "execution policy](security.md).",
    ),
    (
        "Admission, scheduling and limits",
        (
            "worker_count",
            "queue_capacity",
            "history_",
            "run_timeout_seconds",
            "step_timeout_seconds",
            "shutdown_grace_seconds",
            "run_request_max_bytes",
            "agent_output_max_bytes",
            "http_timeout_seconds",
            "http_max_connections",
        ),
        "Bounded queue, workers, deadlines, retained history and payload sizes. See "
        "[Architecture](architecture.md).",
    ),  # fmt: skip
    (
        "Tool retries",
        ("tool_retry_",),
        "Retries for classified transient tool failures, within the run's deadline.",
    ),
    (
        "Agent budgets",
        (
            "agent_max_steps",
            "agent_completion_tokens",
            "agent_request_limit",
            "agent_input_tokens",
            "agent_context_tokens",
            "agent_evidence_bytes",
            "agent_max_cost_usd",
            "model_input_",
            "model_output_",
            "model_price_version",
        ),
        "Step, token, context and cost budgets per run. See [Budgets](budgets.md).",
    ),  # fmt: skip
    (
        "Agents and projects",
        ("agents", "agent_workflow", "project_file", "job_python"),
        "Python agents loaded at startup, the project the workspace's test screens use, and "
        "the interpreter their jobs run with. See [Run your own Python agent](agents.md).",
    ),
    (
        "HTTP agents",
        ("http_agents", "http_agent_"),
        "Agents in their own process, any language. See [HTTP agents](http-agents.md).",
    ),
    (
        "LLM proxy",
        ("proxy_", "key_store"),
        "Mock or passthrough mode, provider keys and limits, faults. See [LLM proxy]"
        "(llm-proxy.md) and the [fault catalog](faults.md).",
    ),
    (
        "Run events",
        ("event_stream_",),
        "The live `/v1/runs/{id}/events` stream.",
    ),
    (
        "Multi-agent coordination",
        ("coordination_",),
        "See [Typed multi-agent coordination](coordination.md).",
    ),
    (
        "Isolated tools",
        ("isolated_tools_", "sandbox_", "browser_allowed_origins"),
        "Disposable Python and browser containers. See [Isolation](isolation.md).",
    ),
    (
        "Built-in model provider",
        (
            "model_provider",
            "model_api_key",
            "model_name",
            "model_concurrency",
            "model_requests_per_minute",
            "groq_",
        ),
        "The built-in demo agent's live model (opt-in, your key). Your own agents use the "
        "[LLM proxy](llm-proxy.md) instead.",
    ),  # fmt: skip
    ("Observability", ("json_logs", "console_traces"), "Structured logs and console traces."),
    ("Demo tools", ("mock_runbook_",), "The built-in demo agent's mock tools."),
    (
        "Durable mode (PostgreSQL)",
        ("database_url", "db_", "lease_seconds", "durable_"),
        "Crash recovery with PostgreSQL. See [Architecture](architecture.md).",
    ),
    (
        "Approvals",
        ("approval_",),
        "See [Human approvals](approvals.md) and [Approvals for your agents](agent-approvals.md).",
    ),
    ("Memory", ("memory_",), "See [Scoped persistent memory](memory.md)."),
]

FAULTS: dict[str, str] = {
    "rate_limit": "HTTP 429 with `Retry-After` (`retry_after` seconds). Not charged.",
    "server_error": "An HTTP 5xx (`status`, default 503). Not charged.",
    "timeout": "The call is held `seconds` (never past the run's deadline), then 504. Not charged.",
    "slow_first_token": "The answer starts `seconds` late, then completes. Charged as usual.",
    "stream_disconnect": "A stream drops after `after_chunks` events; streaming calls only. "
    "The full reservation is kept.",
    "malformed_tool_call": "A tool call whose JSON arguments are cut off; requests with "
    "tools only. Charged as usual.",
    "unknown_tool": "A tool call naming a tool the request did not declare; requests with "
    "tools only. Charged as usual.",
    "context_length": "HTTP 400 `context_length_exceeded` (OpenAI) or `prompt is too long` "
    "(Anthropic). Not charged.",
}


def group_of(name: str) -> str | None:
    for title, members, _ in GROUPS:
        if any(name.startswith(m) if m.endswith("_") else name == m for m in members):
            return title
    return None


def render(text: str, name: str, body: str) -> str:
    pattern = re.compile(
        rf"(<!-- generated:{re.escape(name)} -->\n).*?(<!-- /generated:{re.escape(name)} -->)",
        re.DOTALL,
    )
    if not pattern.search(text):
        raise ValueError(f"no generated:{name} section")
    return pattern.sub(lambda match: match[1] + body + match[2], text, count=1)


# Types, defaults and bounds of pydantic fields


def _type(annotation: Any) -> str:
    origin = typing.get_origin(annotation)
    args = typing.get_args(annotation)
    if origin is typing.Annotated:
        return _type(args[0])
    if origin is typing.Literal:
        return " \\| ".join(f'`"{value}"`' for value in args)
    if origin in (typing.Union, types.UnionType):
        kept = [arg for arg in args if arg is not type(None)]
        return " \\| ".join(_type(arg) for arg in kept)
    if origin in (list, tuple, frozenset, set):
        return "list"
    if origin is dict:
        return "object"
    if annotation is SecretStr:
        return "secret"
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return "object"
    names = {bool: "boolean", int: "integer", float: "number", Decimal: "decimal",
             str: "string", Path: "path"}  # fmt: skip
    return names.get(annotation, getattr(annotation, "__name__", str(annotation)))


def _default(field: FieldInfo) -> str:
    if field.default_factory is not None:
        value = field.default_factory()  # type: ignore[call-arg]
    elif field.default is PydanticUndefined:
        return "required"
    else:
        value = field.default
    if isinstance(value, SecretStr):
        return "empty" if not value.get_secret_value() else "set"
    if value is None:
        return "unset"
    if isinstance(value, bool):
        return f"`{str(value).lower()}`"
    if isinstance(value, (tuple, list, frozenset, set)):
        return "`[" + ", ".join(str(item) for item in value) + "]`" if value else "empty"
    if isinstance(value, dict):
        return "empty" if not value else "`" + str(value) + "`"
    if value == "":
        return "empty"
    return f"`{value}`"


def _bounds(field: FieldInfo) -> str:
    parts = []
    for item in field.metadata:
        for kind, attribute, sign in ((annotated_types.Ge, "ge", "≥"),
                                      (annotated_types.Gt, "gt", ">"),
                                      (annotated_types.Le, "le", "≤"),
                                      (annotated_types.Lt, "lt", "<")):  # fmt: skip
            if isinstance(item, kind):
                parts.append(f"{sign} {getattr(item, attribute)}")
        if isinstance(item, annotated_types.MaxLen):
            parts.append(f"length ≤ {item.max_length}")
        if isinstance(item, annotated_types.MinLen) and item.min_length:
            parts.append(f"length ≥ {item.min_length}")
    return ", ".join(parts)


def _table(header: tuple[str, ...], rows: list[tuple[str, ...]]) -> str:
    lines = ["| " + " | ".join(header) + " |", "| " + " | ".join("---" for _ in header) + " |"]
    lines += ["| " + " | ".join(row) + " |" for row in rows]
    return "\n".join(lines) + "\n"


# Sections


def settings_markdown() -> str:
    from runweave.core.settings import Settings

    out = []
    for title, _, description in GROUPS:
        rows = [
            (f"`RUNWEAVE_{name.upper()}`", _type(field.annotation), _default(field),
             _bounds(field))
            for name, field in Settings.model_fields.items()
            if group_of(name) == title
        ]  # fmt: skip
        out.append(f"### {title}\n\n{description}\n\n")
        out.append(_table(("Variable", "Type", "Default", "Allowed"), rows) + "\n")
    return "".join(out)


def project_markdown() -> str:
    from runweave import project

    sections: list[tuple[str, type[BaseModel], str]] = [
        ("[agent]", project.AgentSection, "The agent to test. Set `python`, or `http` "
         "and/or `command`."),
        ("[limits]", project.LimitsSection, "Per-run deadline and cases in flight."),
        ("[proxy]", project.ProxySection, "The LLM proxy for this project's runs."),
        ("[[datasets]]", project.DatasetSection, "One table per dataset (`.jsonl`, `.yaml` "
         "or `.yml`)."),
        ("[eval]", project.EvalSection, "`runweave eval` gates and report folder."),
        ("[bench]", project.BenchSection, "`runweave bench` sweep and gates."),
        ("[shadow]", project.ShadowSection, "`runweave shadow`: the candidate (an agent "
         "table) and promotion thresholds."),
    ]  # fmt: skip
    out = []
    for title, model, description in sections:
        rows = [
            (f"`{name}`", _type(field.annotation), _default(field), _bounds(field))
            for name, field in model.model_fields.items()
        ]
        out.append(f"### `{title}`\n\n{description}\n\n")
        out.append(_table(("Key", "Type", "Default", "Allowed"), rows) + "\n")
    return "".join(out)


def _arguments(command: argparse.ArgumentParser) -> list[tuple[str, ...]]:
    rows = []
    for action in command._actions:
        if action.help == argparse.SUPPRESS or isinstance(
            action, (argparse._HelpAction, argparse._SubParsersAction)
        ):
            continue
        if action.option_strings:
            name = ", ".join(f"`{option}`" for option in action.option_strings)
            if action.nargs != 0:
                name += f" `{action.metavar or action.dest.upper()}`"
        else:
            name = f"`{action.metavar or action.dest}`"
        default = action.default
        shown = "" if default in (None, False, [], argparse.SUPPRESS) else f"`{default}`"
        rows.append((name, (action.help or "").replace("|", "\\|"), shown))
    return rows


def _commands(command: argparse.ArgumentParser, prefix: str) -> str:
    out = []
    for action in command._actions:
        if not isinstance(action, argparse._SubParsersAction):
            continue
        helps = {choice.dest: choice.help for choice in action._choices_actions}
        for name, sub in action.choices.items():
            full = f"{prefix} {name}"
            out.append(f"### `{full}`\n\n{helps.get(name) or sub.description or ''}\n\n")
            rows = _arguments(sub)
            if rows:
                out.append(_table(("Argument", "What it does", "Default"), rows) + "\n")
            out.append(_commands(sub, full))
    return "".join(out)


def cli_markdown() -> str:
    from runweave.cli import parser

    return _commands(parser(), "runweave")


def faults_markdown() -> str:
    from runweave.proxy.faults import BUILT_IN_PROFILES, FaultKind

    kinds = typing.get_args(FaultKind)
    missing = [kind for kind in kinds if kind not in FAULTS]
    if missing:
        raise ValueError(f"describe these faults in FAULTS: {missing}")
    out = [
        _table(
            ("Kind", "What the agent sees, and the run's budget"),
            [(f"`{k}`", FAULTS[k]) for k in kinds],
        )
    ]
    rows = []
    for name, profile in BUILT_IN_PROFILES.items():
        faults = "; ".join(
            f"`{fault.kind}` {fault.probability:g}"
            + "".join(
                f", {key} {getattr(fault, key):g}"
                for key in ("retry_after", "status", "seconds", "after_chunks")
                if key in fault.model_fields_set
            )
            for fault in profile.faults
        )
        rows.append((f"`{name}`", profile.description, faults))
    out.append("\n### Built-in profiles\n\n")
    out.append(_table(("Profile", "What it does", "Faults (kind, probability, parameters)"),
                      rows))  # fmt: skip
    return "".join(out)


SECTIONS: dict[str, dict[str, Callable[[], str]]] = {
    "docs/cli.md": {"commands": cli_markdown},
    "docs/configuration.md": {"project": project_markdown, "settings": settings_markdown},
    "docs/faults.md": {"faults": faults_markdown},
}


def _rendered(root: Path) -> dict[Path, tuple[str, str]]:
    result = {}
    for page, sections in SECTIONS.items():
        path = root / page
        before = path.read_text(encoding="utf-8")
        after = before
        for name, build in sections.items():
            after = render(after, name, build())
        result[path] = (before, after)
    return result


def stale(root: Path = ROOT) -> list[str]:
    """Pages whose generated sections differ from what the code says now."""
    return [str(path) for path, (before, after) in _rendered(root).items() if before != after]


def main() -> int:
    args = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    args.add_argument("--check", action="store_true", help="Exit 1 if a page is out of date")
    check = args.parse_args().check
    changed = []
    for path, (before, after) in _rendered(ROOT).items():
        if before != after:
            changed.append(path)
            if not check:
                path.write_text(after, encoding="utf-8", newline="\n")
    for path in changed:
        print(f"{'Out of date' if check else 'Updated'}: {path.relative_to(ROOT)}")
    return 1 if check and changed else 0


if __name__ == "__main__":
    sys.exit(main())
