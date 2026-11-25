"""Eval cases (JSONL and YAML) and the built-in, Python and Pydantic Evals assertions."""

import json
from pathlib import Path
from typing import Any

import pytest

from runweave.evals.assertions import CaseResult, check
from runweave.evals.cases import CaseError, load_cases

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {"words": {"type": "integer"}, "longest": {"type": "string"}},
    "required": ["words", "longest"],
}


def result(**overrides: Any) -> CaseResult:
    snapshot: dict[str, Any] = {
        "status": "SUCCEEDED",
        "result": {"words": 3, "longest": "wonderful", "nested": {"note": "all good"}},
        "error": None,
        "steps": [
            {"title": "Split words", "kind": "step", "detail": None, "status": "succeeded"},
            {
                "title": "Tool call",
                "kind": "step",
                "detail": "lookup_weather",
                "status": "succeeded",
            },
            {
                "title": "Model call",
                "kind": "model_call",
                "detail": "chat · mock",
                "status": "succeeded",
            },
        ],
        "usage": {
            "input_tokens": 100,
            "output_tokens": 20,
            "estimated_cost_usd": "0.0012",
            "requests": 1,
        },
    }
    snapshot.update(overrides.pop("snapshot", {}))
    return CaseResult(
        snapshot=snapshot,
        latency_ms=overrides.pop("latency_ms", 120.0),
        output_schema=OUTPUT_SCHEMA,
        **overrides,
    )


def outcomes(expect: dict[str, Any], **overrides: Any) -> dict[str, bool]:
    return {a.name: a.passed for a in check(expect, result(**overrides), input={"text": "hi"})}


def test_jsonl_and_yaml_cases_load_with_ids(tmp_path: Path) -> None:
    jsonl = tmp_path / "cases.jsonl"
    jsonl.write_text(
        json.dumps({"id": "a", "input": {"text": "x"}, "expect": {"output": {"words": 1}}})
        + "\n\n"
        + json.dumps({"input": {"text": "y"}, "tags": ["slow"]})
        + "\n",
        encoding="utf-8",
    )
    yaml = tmp_path / "cases.yaml"
    yaml.write_text(
        "- id: b\n  input: {text: z}\n  expect: {status: SUCCEEDED}\n", encoding="utf-8"
    )
    first = load_cases(jsonl)
    assert [c.id for c in first] == ["a", "case-3"]
    assert first[1].tags == ("slow",)
    assert [c.id for c in load_cases(yaml)] == ["b"]


@pytest.mark.parametrize(
    ("name", "content", "message"),
    [
        ("bad.jsonl", '{"input": {}}\n{nope\n', "line 2"),
        ("dupe.jsonl", '{"id": "a", "input": {}}\n{"id": "a", "input": {}}\n', "duplicate"),
        ("shape.jsonl", '{"id": "a", "input": [1]}\n', "line 1"),
        ("unknown.jsonl", '{"id": "a", "input": {}, "expect": {"frobnicate": 1}}\n', "frobnicate"),
        ("list.yaml", "id: a\n", "list of cases"),
        ("empty.jsonl", "\n", "no cases"),
        ("cases.txt", "{}", ".jsonl"),
    ],
)
def test_invalid_case_files_point_at_the_problem(
    tmp_path: Path, name: str, content: str, message: str
) -> None:
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    with pytest.raises(CaseError, match=message):
        load_cases(path)


def test_output_schema_and_status_assertions() -> None:
    assert outcomes({}) == {"status": True, "schema": True}
    assert outcomes({}, snapshot={"result": {"words": "three"}})["schema"] is False
    assert outcomes({"status": "FAILED"})["status"] is False
    failed = {"status": "FAILED", "result": None, "error": {"code": "AGENT_STEP_LIMIT"}}
    assert outcomes({"error_code": "AGENT_STEP_LIMIT"}, snapshot=failed) == {
        "status": True,
        "error_code": True,
    }


def test_output_matching_assertions() -> None:
    assert outcomes({"output": {"words": 3}})["output"] is True
    assert outcomes({"output": {"words": 4}})["output"] is False
    assert outcomes({"output_equals": {"words": 3}})["output_equals"] is False
    assert outcomes({"contains": {"longest": "wonder", "nested.note": "good"}})["contains"] is True
    assert outcomes({"contains": {"nested.missing": "x"}})["contains"] is False
    assert outcomes({"matches": {"longest": "^won.*ful$"}})["matches"] is True


def test_tool_and_canary_assertions() -> None:
    assert outcomes({"tool_called": ["Split words", "lookup_weather"]})["tool_called"] is True
    assert outcomes({"tool_called": ["delete_everything"]})["tool_called"] is False
    assert outcomes({"not_contains": ["RW_CANARY_1"]})["not_contains"] is True
    leaked = {"result": {"words": 1, "longest": "x", "debug": "RW_CANARY_1"}}
    assert outcomes({"not_contains": ["RW_CANARY_1"]}, snapshot=leaked)["not_contains"] is False


def test_latency_cost_token_and_model_call_ceilings() -> None:
    within = {"max_latency_ms": 500, "max_cost_usd": 0.01, "max_tokens": 200, "max_model_calls": 1}
    assert set(outcomes(within).values()) == {True}
    over = outcomes(
        {"max_latency_ms": 100, "max_cost_usd": 0.001, "max_tokens": 50, "max_model_calls": 0},
        latency_ms=150.0,
    )
    assert over == {
        "status": True,
        "schema": True,
        "max_latency_ms": False,
        "max_cost_usd": False,
        "max_tokens": False,
        "max_model_calls": False,
    }


def test_messages_explain_failures_without_dumping_whole_outputs() -> None:
    failures = [a for a in check({"output": {"words": 4}}, result(), input={}) if not a.passed]
    assert "words" in failures[0].message and len(failures[0].message) < 300


def test_custom_python_assertions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "checks.py").write_text(
        "def has_three(case, result):\n    return result.output['words'] == 3\n"
        "def explained(case, result):\n    return False, 'needs more words'\n"
        "def broken(case, result):\n    raise RuntimeError('boom')\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    found = {
        a.name: a
        for a in check(
            {"python": ["checks:has_three", "checks:explained", "checks:broken"]},
            result(),
            input={},
        )
    }
    assert found["python:checks:has_three"].passed is True
    assert (found["python:checks:explained"].passed, found["python:checks:explained"].message) == (
        False,
        "needs more words",
    )
    assert found["python:checks:broken"].passed is False
    assert "RuntimeError" in found["python:checks:broken"].message


def test_pydantic_evals_evaluators_run_on_the_result() -> None:
    pytest.importorskip("pydantic_evals")
    found = {
        a.name: a.passed
        for a in check(
            {
                "pydantic_evals": [
                    # On a dict output, pydantic-evals Contains checks keys.
                    {"evaluator": "Contains", "args": {"value": "longest"}},
                    {"evaluator": "MaxDuration", "args": {"seconds": 0.05}},
                ]
            },
            result(),
            input={"text": "hi"},
        )
    }
    assert found["pydantic_evals:Contains"] is True
    assert found["pydantic_evals:MaxDuration"] is False  # 120 ms > 50 ms


def effect(
    name: str, operation: str, resource: str | None, fault: str | None = None
) -> dict[str, Any]:
    return {
        "title": name, "kind": "effect", "effect": name, "operation": operation,
        "resource": resource, "fault": fault, "detail": None,
        "status": "failed" if fault else "succeeded",
    }  # fmt: skip


def test_exactly_once_fails_a_retry_that_wrote_twice() -> None:
    twice = {"steps": [effect("create_ticket", "op", "r1", "response_lost"),
                       effect("create_ticket", "op", "r2")]}  # fmt: skip
    once = {"steps": [effect("create_ticket", "op", "r1", "response_lost"),
                      effect("create_ticket", "op", "r1")]}  # fmt: skip
    assert outcomes({"exactly_once": True}, snapshot=once)["exactly_once"] is True
    assert outcomes({"exactly_once": True}, snapshot=twice)["exactly_once"] is False
    assert outcomes({"exactly_once": ["send_email"]}, snapshot=twice)["exactly_once"] is True
    [failure] = [
        a for a in check({"exactly_once": ["create_ticket"]}, result(snapshot=twice), input={})
        if a.name == "exactly_once"
    ]  # fmt: skip
    assert not failure.passed and "create_ticket" in failure.message


def test_tool_called_sees_effects() -> None:
    steps = {"steps": [effect("create_ticket", "op", "r1")]}
    assert outcomes({"tool_called": ["create_ticket"]}, snapshot=steps)["tool_called"] is True


def test_max_tool_calls_limits_each_named_tool() -> None:
    steps = {"steps": [effect("refund", "op-1", "r1"), effect("refund", "op-2", "r2"),
                       effect("send_email", "op", "r")]}  # fmt: skip
    assert outcomes({"max_tool_calls": {"refund": 2}}, snapshot=steps)["max_tool_calls"] is True
    assert outcomes({"max_tool_calls": {"absent": 0}}, snapshot=steps)["max_tool_calls"] is True
    [failure] = [
        a
        for a in check(
            {"max_tool_calls": {"refund": 1, "send_email": 1}}, result(snapshot=steps), input={}
        )
        if a.name == "max_tool_calls"
    ]
    assert not failure.passed and failure.message == "refund called 2 times, at most 1"


def test_a_negative_call_limit_is_rejected() -> None:
    with pytest.raises(ValueError):
        check({"max_tool_calls": {"refund": -1}}, result(), input={})
