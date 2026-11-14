import json
import sys
from pathlib import Path
from typing import cast

import pytest

pytest.importorskip("pydantic_evals")

from runweave.evaluation.suite import (  # noqa: E402
    CANARY,
    Observation,
    Regression,
    Workflow,
    check_observation,
    evaluate,
    execute_fixture,
    load_fixtures,
    main,
    seed_regression,
)


@pytest.mark.parametrize("workflow", ["baseline", "pydanticai"])
async def test_offline_evaluation_is_reproducible(workflow: Workflow) -> None:
    if workflow == "pydanticai":
        pytest.importorskip("pydantic_ai")
    first = await evaluate(workflow)
    assert first["passed"] is True
    assert first == await evaluate(workflow)
    assert len(cast(list[object], first["cases"])) == len(load_fixtures()[0].cases)


@pytest.mark.parametrize("workflow", ["baseline", "pydanticai"])
@pytest.mark.parametrize("regression", ["severity", "evidence", "claims", "secret", "tool"])
async def test_evaluation_fails_seeded_regressions(
    workflow: Workflow, regression: Regression
) -> None:
    if workflow == "pydanticai":
        pytest.importorskip("pydantic_ai")
    report = await evaluate(workflow, regression)
    assert report["passed"] is False
    assert report["task_failures"] == []
    assert CANARY not in json.dumps(report)


async def test_checks_catch_specific_corruptions() -> None:
    fixture = load_fixtures()[0].cases[0]
    observed = await execute_fixture(fixture, "baseline")
    assert all(check_observation(fixture, observed).values())
    expected: dict[Regression, str] = {
        "severity": "severity",
        "evidence": "evidence",
        "claims": "unsupported_claims",
        "secret": "secret_absent",
        "tool": "tool_choice",
    }
    for regression, check in expected.items():
        assert not check_observation(fixture, seed_regression(observed, regression))[check]
    assert not check_observation(fixture, Observation())["outcome"]


@pytest.mark.parametrize("seed", [False, True])
def test_cli_exit_status_and_report(
    seed: bool, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "eval.json"
    arguments = ["eval", "--output", str(output)]
    if seed:
        arguments += ["--seed-regression", "severity"]
    monkeypatch.setattr(sys, "argv", arguments)
    assert main() == int(seed)
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["passed"] is (not seed)
    assert json.loads(capsys.readouterr().out) == report


async def test_unexpected_execution_errors_fail_the_report(monkeypatch: pytest.MonkeyPatch) -> None:
    async def broken(fixture: object, workflow: object) -> Observation:
        raise RuntimeError("seeded infrastructure failure")

    monkeypatch.setattr("runweave.evaluation.suite.execute_fixture", broken)
    report = await evaluate("baseline")
    assert report["passed"] is False
    assert len(cast(list[object], report["task_failures"])) == len(load_fixtures()[0].cases)


async def test_evaluator_errors_fail_the_report(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(fixture: object, output: object) -> dict[str, bool]:
        raise RuntimeError("seeded evaluator failure")

    monkeypatch.setattr("runweave.evaluation.suite.check_observation", broken)
    report = await evaluate("baseline")
    assert report["passed"] is False
    assert report["task_failures"] == []
