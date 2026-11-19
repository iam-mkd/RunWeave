"""Eval cases from JSONL (one case per line) or YAML (a list of cases).

{"id": "short", "input": {"text": "hi"}, "expect": {"output": {"words": 1}}, "tags": ["smoke"]}
"""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from runweave.evals.assertions import Expectations

MAX_CASES = 10_000
MAX_FILE_BYTES = 32 * 1024 * 1024


class CaseError(ValueError):
    pass


class _CaseModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,99}$")
    input: dict[str, Any]
    expect: Expectations = Field(default_factory=Expectations)
    expected_output: Any = None
    tags: tuple[str, ...] = Field(default=(), max_length=20)
    metadata: dict[str, Any] = Field(default_factory=dict)


@dataclass(frozen=True)
class Case:
    id: str
    input: dict[str, Any]
    expect: Expectations
    expected_output: Any = None
    tags: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)


def _problem(error: ValidationError) -> str:
    item = error.errors(include_url=False, include_input=False, include_context=False)[0]
    where = ".".join(str(p) for p in item["loc"])
    return f"{where}: {item['msg']}" if where else item["msg"]


def _records(path: Path) -> list[tuple[int, Any]]:
    if path.stat().st_size > MAX_FILE_BYTES:
        raise CaseError(f"{path.name} is larger than 32 MiB")
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".jsonl":
        records = []
        for number, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                records.append((number, json.loads(line)))
            except json.JSONDecodeError as error:
                raise CaseError(
                    f"{path.name} line {number}: not valid JSON ({error.msg})"
                ) from None
        return records
    if path.suffix in {".yaml", ".yml"}:
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as error:
            raise CaseError(f"{path.name}: not valid YAML ({error})") from None
        if not isinstance(data, list):
            raise CaseError(f"{path.name} must contain a list of cases")
        return [(index, item) for index, item in enumerate(data, start=1)]
    raise CaseError(f"{path.name}: use .jsonl, .yaml or .yml")


def load_cases(path: Path) -> list[Case]:
    try:
        records = _records(path)
    except OSError as error:
        raise CaseError(f"Cannot read {path}: {error.strerror}") from None
    unit = "line" if path.suffix == ".jsonl" else "case"
    cases: list[Case] = []
    seen: set[str] = set()
    for number, record in records:
        try:
            parsed = _CaseModel.model_validate(record)
        except ValidationError as error:
            raise CaseError(f"{path.name} {unit} {number}: {_problem(error)}") from None
        case_id = parsed.id or f"case-{number}"
        if case_id in seen:
            raise CaseError(f"{path.name} {unit} {number}: duplicate id {case_id!r}")
        seen.add(case_id)
        cases.append(
            Case(
                case_id,
                parsed.input,
                parsed.expect,
                parsed.expected_output,
                parsed.tags,
                parsed.metadata,
            )
        )
        if len(cases) > MAX_CASES:
            raise CaseError(f"{path.name} has more than {MAX_CASES} cases")
    if not cases:
        raise CaseError(f"{path.name} has no cases")
    return cases
