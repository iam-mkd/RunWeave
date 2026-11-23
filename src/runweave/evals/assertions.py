"""Assertions an eval case can make about one finished run.

Built-in: status, error_code, schema (the agent's output schema, on by default for
successful runs), output (subset), output_equals, contains / matches (dotted paths),
tool_called (step titles, or the built-in agent's "Tool call" steps), not_contains
(canaries anywhere in the output or timeline), max_latency_ms, max_cost_usd, max_tokens,
max_model_calls and exactly_once (no duplicate write after a lost response). Also custom
Python checks (`module:function`, called with the case and the result; return a bool or
(bool, message), or raise) and Pydantic Evals evaluators.
"""

import importlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal

from jsonschema import Draft202012Validator
from pydantic import BaseModel, ConfigDict, Field

from runweave.agent_effects import duplicate_writes

# REJECTED: refused at admission (for example invalid input), so no run exists.
Status = Literal["SUCCEEDED", "FAILED", "TIMED_OUT", "CANCELLED", "REJECTED"]
TARGET = r"^[A-Za-z_][\w.]*:[A-Za-z_]\w*$"


class EvaluatorSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evaluator: str = Field(pattern=r"^[A-Za-z_][\w.:]*$", max_length=200)
    args: dict[str, Any] = Field(default_factory=dict)


class Expectations(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Status | None = None
    error_code: str | None = Field(default=None, max_length=64)
    schema_: bool | None = Field(default=None, alias="schema")
    output: dict[str, Any] | None = None
    output_equals: Any = None
    contains: dict[str, str] | None = None
    matches: dict[str, str] | None = None
    tool_called: list[str] | None = Field(default=None, max_length=50)
    not_contains: list[str] | None = Field(default=None, max_length=50)
    max_latency_ms: float | None = Field(default=None, gt=0)
    max_cost_usd: Decimal | None = Field(default=None, ge=0)
    max_tokens: int | None = Field(default=None, ge=0)
    max_model_calls: int | None = Field(default=None, ge=0)
    # No operation of these effects (True: of every effect) produced two resources in a run:
    # a retry after a lost response did not write twice. See ctx.effect.
    exactly_once: bool | list[str] | None = Field(default=None)
    python: list[str] | None = Field(default=None, max_length=20)
    pydantic_evals: list[EvaluatorSpec] | None = Field(default=None, max_length=20)

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


@dataclass(frozen=True)
class CaseResult:
    snapshot: dict[str, Any]
    latency_ms: float
    output_schema: dict[str, Any] | None = None

    @property
    def output(self) -> Any:
        return self.snapshot.get("result")

    @property
    def status(self) -> str:
        return str(self.snapshot.get("status"))

    @property
    def steps(self) -> list[dict[str, Any]]:
        return list(self.snapshot.get("steps") or [])

    @property
    def usage(self) -> dict[str, Any]:
        return dict(self.snapshot.get("usage") or {})


@dataclass(frozen=True)
class Outcome:
    name: str
    passed: bool
    message: str = ""


def _clip(value: Any, limit: int = 120) -> str:
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _path(value: Any, dotted: str) -> tuple[bool, Any]:
    for part in dotted.split("."):
        if isinstance(value, dict) and part in value:
            value = value[part]
        elif isinstance(value, list) and part.isdigit() and int(part) < len(value):
            value = value[int(part)]
        else:
            return False, None
    return True, value


def _subset(expected: Any, actual: Any, where: str = "") -> str | None:
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            return f"{where or 'output'} is not an object"
        for key, value in expected.items():
            here = f"{where}.{key}" if where else key
            if key not in actual:
                return f"{here} is missing"
            problem = _subset(value, actual[key], here)
            if problem:
                return problem
        return None
    if expected != actual:
        return f"{where or 'output'} is {_clip(actual, 60)}, expected {_clip(expected, 60)}"
    return None


def _import(target: str) -> Any:
    module, attribute = target.split(":")
    return getattr(importlib.import_module(module), attribute)


def _python(target: str, case: Any, result: CaseResult) -> Outcome:
    name = f"python:{target}"
    if not re.fullmatch(TARGET, target):
        return Outcome(name, False, "use module:function")
    try:
        verdict = _import(target)(case, result)
    except AssertionError as error:
        return Outcome(name, False, _clip(str(error) or "assertion failed", 200))
    except Exception as error:  # noqa: BLE001 - a broken check fails its case, not the run
        return Outcome(name, False, _clip(f"{type(error).__name__}: {error}", 200))
    if isinstance(verdict, tuple):
        passed, message = bool(verdict[0]), str(verdict[1]) if len(verdict) > 1 else ""
        return Outcome(name, passed, _clip(message, 200))
    return Outcome(name, bool(verdict))


def _pydantic_evals(
    spec: EvaluatorSpec, case: Any, inputs: Any, result: CaseResult
) -> list[Outcome]:
    label = spec.evaluator.rsplit(":", 1)[-1].rsplit(".", 1)[-1]
    name = f"pydantic_evals:{label}"
    try:
        from pydantic_evals import evaluators as built_in
        from pydantic_evals.evaluators import EvaluatorContext
        from pydantic_evals.otel._errors import SpanTreeRecordingError
    except ImportError:
        return [Outcome(name, False, "install the eval extra: pip install 'runweave[eval]'")]
    try:
        cls = (
            _import(spec.evaluator) if ":" in spec.evaluator else getattr(built_in, spec.evaluator)
        )
        evaluator = cls(**spec.args)
        context = EvaluatorContext(
            name=getattr(case, "id", None),
            inputs=inputs,
            metadata=getattr(case, "metadata", None),
            expected_output=getattr(case, "expected_output", None),
            output=result.output,
            duration=result.latency_ms / 1000,
            _span_tree=SpanTreeRecordingError("Spans are not recorded by runweave eval"),
            attributes={},
            metrics={},
        )
        verdict = evaluator.evaluate(context)
    except Exception as error:  # noqa: BLE001 - an evaluator failure fails its case
        return [Outcome(name, False, _clip(f"{type(error).__name__}: {error}", 200))]
    values = verdict if isinstance(verdict, dict) else {name: verdict}
    outcomes = []
    for key, value in values.items():
        reason = getattr(value, "reason", None) or ""
        passed = value.value if hasattr(value, "value") else value
        label_name = key if key == name else f"{name}.{key}"
        outcomes.append(
            Outcome(
                label_name,
                bool(passed) if isinstance(passed, bool) else False,
                _clip(
                    reason
                    or (
                        ""
                        if isinstance(passed, bool)
                        else f"non-boolean result {_clip(passed, 40)}"
                    ),
                    200,
                ),
            )
        )
    return outcomes


Check = Callable[[Any, CaseResult], Outcome]


def check(
    expect: Expectations | dict[str, Any], result: CaseResult, *, input: Any, case: Any = None
) -> list[Outcome]:
    e = expect if isinstance(expect, Expectations) else Expectations.model_validate(expect)
    out: list[Outcome] = []
    wanted = e.status or ("FAILED" if e.error_code else "SUCCEEDED")
    out.append(
        Outcome(
            "status",
            result.status == wanted,
            "" if result.status == wanted else f"status is {result.status}, expected {wanted}",
        )
    )
    if e.error_code is not None:
        code = (result.snapshot.get("error") or {}).get("code")
        out.append(
            Outcome(
                "error_code",
                code == e.error_code,
                "" if code == e.error_code else f"error code is {code}, expected {e.error_code}",
            )
        )
    schema_wanted = e.schema_ if e.schema_ is not None else (wanted == "SUCCEEDED")
    if schema_wanted and result.output_schema is not None:
        if result.status == "SUCCEEDED":
            errors = list(Draft202012Validator(result.output_schema).iter_errors(result.output))
            message = ""
            if errors:
                where = "/".join(map(str, errors[0].absolute_path)) or "root"
                message = (
                    f"output does not match the schema at {where} ({errors[0].validator} rule)"
                )
            out.append(Outcome("schema", not errors, message))
        elif e.schema_:
            out.append(Outcome("schema", False, "no output to check"))
    if e.output is not None:
        problem = _subset(e.output, result.output)
        out.append(Outcome("output", problem is None, problem or ""))
    if "output_equals" in e.model_fields_set:
        equal = result.output == e.output_equals
        out.append(
            Outcome(
                "output_equals",
                equal,
                ""
                if equal
                else f"output is {_clip(result.output)}, expected {_clip(e.output_equals)}",
            )
        )
    if e.contains:
        problems = []
        for dotted, text in e.contains.items():
            found, value = _path(result.output, dotted)
            if not found:
                problems.append(f"{dotted} is missing")
            elif text not in (value if isinstance(value, str) else json.dumps(value)):
                problems.append(f"{dotted} does not contain {_clip(text, 40)}")
        out.append(Outcome("contains", not problems, "; ".join(problems)))
    if e.matches:
        problems = []
        for dotted, pattern in e.matches.items():
            found, value = _path(result.output, dotted)
            if not found or not isinstance(value, str) or re.search(pattern, value) is None:
                problems.append(f"{dotted} does not match {_clip(pattern, 40)}")
        out.append(Outcome("matches", not problems, "; ".join(problems)))
    if e.tool_called:
        called = {s.get("title") for s in result.steps} | {
            s.get("detail") for s in result.steps if s.get("title") == "Tool call"
        }
        missing = [name for name in e.tool_called if name not in called]
        out.append(
            Outcome(
                "tool_called", not missing, f"not called: {', '.join(missing)}" if missing else ""
            )
        )
    if e.not_contains:
        text = json.dumps(result.output, default=str) + json.dumps(
            [[s.get("title"), s.get("detail")] for s in result.steps]
        )
        leaked = [canary for canary in e.not_contains if canary in text]
        out.append(
            Outcome(
                "not_contains",
                not leaked,
                f"found {len(leaked)} forbidden value(s) in the output or timeline"
                if leaked
                else "",
            )
        )
    usage = result.usage
    if e.max_latency_ms is not None:
        ok = result.latency_ms <= e.max_latency_ms
        out.append(
            Outcome(
                "max_latency_ms",
                ok,
                "" if ok else f"took {result.latency_ms:.0f} ms, limit {e.max_latency_ms:.0f} ms",
            )
        )
    if e.max_cost_usd is not None:
        cost = Decimal(str(usage.get("estimated_cost_usd") or 0))
        ok = cost <= e.max_cost_usd
        out.append(
            Outcome("max_cost_usd", ok, "" if ok else f"cost ${cost}, limit ${e.max_cost_usd}")
        )
    if e.max_tokens is not None:
        tokens = int(usage.get("input_tokens") or 0) + int(usage.get("output_tokens") or 0)
        ok = tokens <= e.max_tokens
        out.append(
            Outcome("max_tokens", ok, "" if ok else f"{tokens} tokens, limit {e.max_tokens}")
        )
    if e.exactly_once:
        names = None if e.exactly_once is True else e.exactly_once
        doubled = duplicate_writes(result.steps, names)
        out.append(
            Outcome(
                "exactly_once",
                not doubled,
                f"wrote twice after a retry: {', '.join(doubled)}" if doubled else "",
            )
        )
    if e.max_model_calls is not None:
        calls = sum(s.get("kind") == "model_call" for s in result.steps)
        ok = calls <= e.max_model_calls
        out.append(
            Outcome(
                "max_model_calls",
                ok,
                "" if ok else f"{calls} model calls, limit {e.max_model_calls}",
            )
        )
    for target in e.python or []:
        out.append(_python(target, case, result))
    for spec in e.pydantic_evals or []:
        out.extend(_pydantic_evals(spec, case, input, result))
    return out
