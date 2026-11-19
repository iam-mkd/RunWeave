"""`runweave eval`: run a dataset through the project's agent and decide pass or fail."""

import asyncio
import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from runweave.evals import report as reports
from runweave.evals.assertions import check
from runweave.evals.cases import Case, load_cases
from runweave.evals.runner import Target, environment, open_target, run_case
from runweave.project import Project, ProjectError


@dataclass(frozen=True)
class EvalOptions:
    dataset: str | None = None
    server: str | None = None
    baseline: Path | None = None
    save_baseline: bool = False
    junit: Path | None = None


async def _evaluate(target: Target, cases: list[Case], concurrency: int) -> list[dict[str, Any]]:
    slots = asyncio.Semaphore(concurrency)

    async def one(case: Case) -> dict[str, Any]:
        async with slots:
            result = await run_case(target, case.input)
        outcomes = check(case.expect, result, input=case.input, case=case)
        usage = result.usage
        return {
            "id": case.id,
            "passed": all(o.passed for o in outcomes),
            "status": result.status,
            "run_id": result.snapshot.get("id"),
            "latency_ms": round(result.latency_ms, 1),
            "tokens": int(usage.get("input_tokens") or 0) + int(usage.get("output_tokens") or 0),
            "cost_usd": str(Decimal(str(usage.get("estimated_cost_usd") or 0))),
            "tags": list(case.tags),
            "assertions": [
                {"name": o.name, "passed": o.passed, "message": o.message} for o in outcomes
            ],
        }

    return list(await asyncio.gather(*(one(case) for case in cases)))


def _load_baseline(path: Path | None) -> dict[str, Any] | None:
    if path is None or not path.is_file():
        return None
    try:
        data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ProjectError(f"The baseline {path} is not a readable eval report") from None
    if data.get("kind") != "eval":
        raise ProjectError(f"The baseline {path} is not an eval report")
    return data


async def run_eval(project: Project, options: EvalOptions) -> tuple[dict[str, Any], Path]:
    dataset = project.dataset(options.dataset)
    cases = load_cases(dataset.path)
    baseline_path = options.baseline or project.eval.baseline
    baseline = (
        None
        if options.save_baseline and options.baseline is None
        else _load_baseline(baseline_path)
    )
    started = time.perf_counter()
    async with open_target(project, options.server) as target:
        results = await _evaluate(target, cases, project.limits.concurrency)
        agent = target.agent
        mode = target.mode
    report: dict[str, Any] = {
        "kind": "eval",
        "version": reports.REPORT_VERSION,
        "id": reports.new_id("eval"),
        "created_at": datetime.now(UTC).isoformat(),
        "dataset": {
            "name": dataset.name,
            "path": str(dataset.path.relative_to(project.root)),
            "sha256": reports.file_digest(dataset.path),
            "cases": len(cases),
        },
        "agent": {"name": agent["name"], "version": agent["version"], "kind": agent["kind"]},
        "environment": environment(mode),
        "settings": {
            "fault_profile": project.proxy.fault_profile,
            "fault_seed": project.proxy.fault_seed,
            "run_timeout_seconds": project.limits.run_timeout_seconds,
            "concurrency": project.limits.concurrency,
        },
        "summary": reports.summarize(results, (time.perf_counter() - started) * 1000),
        "cases": results,
    }
    report["baseline"] = reports.compare(report, baseline, baseline_path if baseline else None)
    report["gates"] = reports.gates(report, project.eval.min_pass_rate)
    folder = reports.write(report, project.eval.reports)
    if options.junit is not None:
        junit_path = options.junit if options.junit.is_absolute() else project.root / options.junit
        junit_path.parent.mkdir(parents=True, exist_ok=True)
        junit_path.write_bytes(reports.junit(report))
    if options.save_baseline:
        target_path = (
            baseline_path or project.root / ".runweave" / "baselines" / f"{dataset.name}.json"
        )
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report, folder


def summary_text(report: dict[str, Any], folder: Path) -> str:
    s, verdict = report["summary"], report["gates"]
    lines = [
        f"{report['agent']['name']} on {report['dataset']['name']}: {s['passed']}/{s['total']} "
        f"cases passed ({s['pass_rate']:.0%}), p95 {s['p95_latency_ms']} ms, "
        f"{s['tokens']} tokens, ${s['cost_usd']}"
    ]
    for case in report["cases"]:
        if not case["passed"]:
            why = "; ".join(
                f"{a['name']}: {a['message']}" for a in case["assertions"] if not a["passed"]
            )
            lines.append(f"  FAILED {case['id']}: {why}")
    baseline = report.get("baseline")
    if baseline and baseline["regressions"]:
        lines.append(f"  Regressed against the baseline: {', '.join(baseline['regressions'])}")
    lines.append(
        "Gates passed." if verdict["passed"] else "Gates failed: " + "; ".join(verdict["reasons"])
    )
    lines.append(f"Report: {folder}")
    return "\n".join(lines)
