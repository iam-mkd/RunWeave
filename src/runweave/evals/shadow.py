"""`runweave shadow`: compare a candidate agent version with the current one, case by case.

Both versions get the same inputs, each in its own RunWeave with its own budgets, at the
same time so they see the same machine load. Each pair records paired differences, and the
promotion gates follow milestone 16: the candidate meets the pass rate, no case the current
version passes fails on the candidate, outcomes match (and outputs, when asked), cost stays
within 105% per case, and p95 latency stays within twice the current p95 plus 50 ms. The
report says whether the candidate is eligible; nothing is switched automatically.
"""

import asyncio
import json
import re
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from runweave.evals import report as reports
from runweave.evals.cases import Case, load_cases
from runweave.evals.command import case_record
from runweave.evals.runner import Target, environment, open_target, run_case
from runweave.project import CandidateSection, Project, ProjectError, ShadowSection

PYTHON_TARGET = re.compile(r"^[A-Za-z_][\w.]*:[A-Za-z_]\w*$")


@dataclass(frozen=True)
class ShadowOptions:
    dataset: str | None = None
    server: str | None = None
    candidate: str | None = None
    progress: reports.Progress = reports.silent


def differences(
    primary: dict[str, Any], candidate: dict[str, Any], *, output_equal: bool
) -> dict[str, Any]:
    before = {a["name"]: a["passed"] for a in primary["assertions"]}
    return {
        "status_equal": primary["status"] == candidate["status"],
        "output_equal": output_equal,
        "latency_ms": round(candidate["latency_ms"] - primary["latency_ms"], 1),
        "tokens": candidate["tokens"] - primary["tokens"],
        "cost_usd": str(Decimal(candidate["cost_usd"]) - Decimal(primary["cost_usd"])),
        "model_calls": candidate["model_calls"] - primary["model_calls"],
        "newly_failing": sorted(
            a["name"] for a in candidate["assertions"] if not a["passed"] and before.get(a["name"])
        ),
        "regressed": primary["passed"] and not candidate["passed"],
        "fixed": candidate["passed"] and not primary["passed"],
    }


def _p95(records: list[dict[str, Any]]) -> float | None:
    return reports.percentile([r["latency_ms"] for r in records if r["status"] != "REJECTED"], 0.95)


def gates(
    pairs: list[dict[str, Any]], policy: ShadowSection, min_pass_rate: float
) -> dict[str, bool]:
    primary = [p["primary"] for p in pairs]
    candidate = [p["candidate"] for p in pairs]
    ratio = Decimal(str(policy.max_cost_ratio))
    before, after = _p95(primary), _p95(candidate)
    return {
        "candidate_pass_rate_met": bool(pairs)
        and sum(c["passed"] for c in candidate) / len(pairs) >= min_pass_rate,
        "no_paired_regressions": not any(p["differences"]["regressed"] for p in pairs),
        "outcome_parity": all(
            p["differences"]["status_equal"]
            and (p["differences"]["output_equal"] or not policy.require_output_parity)
            for p in pairs
        ),
        "candidate_cost_within_limit": all(
            Decimal(p["candidate"]["cost_usd"]) <= Decimal(p["primary"]["cost_usd"]) * ratio
            for p in pairs
        ),
        "candidate_latency_within_limit": before is None
        or after is None
        or after <= before * policy.max_latency_ratio + policy.latency_slack_ms,
    }


def _candidate(project: Project, options: ShadowOptions) -> CandidateSection:
    if options.candidate is not None:
        value = options.candidate
        if PYTHON_TARGET.match(value):
            return CandidateSection(python=value)
        return CandidateSection(http=value)
    if project.shadow.candidate is None:
        raise ProjectError(
            "Name a candidate to compare: add [shadow] candidate = { python = "
            f'"agent_v2:run" }} to {project.file.name}, or pass --candidate'
        )
    return project.shadow.candidate


async def _pairs(
    primary: Target,
    candidate: Target,
    cases: list[Case],
    concurrency: int,
    progress: reports.Progress,
) -> list[dict[str, Any]]:
    slots = asyncio.Semaphore(concurrency)
    done = 0

    async def one(case: Case) -> dict[str, Any]:
        nonlocal done
        async with slots:
            before, after = await asyncio.gather(
                run_case(primary, case.input), run_case(candidate, case.input)
            )
        old, new = case_record(case, before), case_record(case, after)
        diff = differences(old, new, output_equal=before.output == after.output)
        done += 1
        progress(
            {
                "event": "pair",
                "id": case.id,
                "regressed": diff["regressed"],
                "fixed": diff["fixed"],
                "status_equal": diff["status_equal"],
                "output_equal": diff["output_equal"],
                "done": done,
                "total": len(cases),
            }
        )
        return {"id": case.id, "primary": old, "candidate": new, "differences": diff}

    return list(await asyncio.gather(*(one(case) for case in cases)))


def _side(target_agent: dict[str, Any], records: list[dict[str, Any]]) -> dict[str, Any]:
    passed = sum(r["passed"] for r in records)
    return {
        "agent": {
            "name": target_agent["name"],
            "version": target_agent["version"],
            "kind": target_agent["kind"],
        },
        "passed": passed,
        "pass_rate": round(passed / len(records), 4) if records else 0.0,
        "p50_latency_ms": reports.percentile(
            [r["latency_ms"] for r in records if r["status"] != "REJECTED"], 0.5
        ),
        "p95_latency_ms": _p95(records),
        "tokens": sum(r["tokens"] for r in records),
        "cost_usd": str(sum((Decimal(r["cost_usd"]) for r in records), Decimal(0))),
    }


async def run_shadow(project: Project, options: ShadowOptions) -> tuple[dict[str, Any], Path]:
    candidate_section = _candidate(project, options)
    if options.server is not None:
        if candidate_section.python is not None:
            raise ProjectError(
                "With --server, the candidate must be an HTTP agent (http or command) "
                "registered under its own name; set [shadow] candidate name"
            )
        if candidate_section.name in (None, project.agent.name):
            raise ProjectError(
                "With --server, give the candidate its own name ([shadow] candidate name) so "
                "both versions can be registered on one server"
            )
    dataset = project.dataset(options.dataset)
    cases = load_cases(dataset.path)
    started = time.perf_counter()
    async with AsyncExitStack() as stack:
        primary = await stack.enter_async_context(open_target(project, options.server))
        candidate = await stack.enter_async_context(
            open_target(project, options.server, agent=candidate_section)
        )
        options.progress({"event": "started", "kind": "shadow", "total": len(cases)})
        pairs = await _pairs(
            primary, candidate, cases, project.limits.concurrency, options.progress
        )
        mode = primary.mode
        agents = primary.agent, candidate.agent
    verdict = gates(pairs, project.shadow, project.eval.min_pass_rate)
    report: dict[str, Any] = {
        "kind": "shadow",
        "version": reports.REPORT_VERSION,
        "id": reports.new_id("shadow"),
        "created_at": datetime.now(UTC).isoformat(),
        "policy": "runweave-shadow-v1 (milestone 16 gates)",
        "dataset": {
            "name": dataset.name,
            "path": str(dataset.path.relative_to(project.root)),
            "sha256": reports.file_digest(dataset.path),
            "cases": len(cases),
        },
        "environment": environment(mode),
        "settings": {
            "fault_profile": project.proxy.fault_profile,
            "fault_seed": project.proxy.fault_seed,
            "concurrency": project.limits.concurrency,
            "min_pass_rate": project.eval.min_pass_rate,
            "max_cost_ratio": project.shadow.max_cost_ratio,
            "max_latency_ratio": project.shadow.max_latency_ratio,
            "latency_slack_ms": project.shadow.latency_slack_ms,
            "require_output_parity": project.shadow.require_output_parity,
        },
        "primary": _side(agents[0], [p["primary"] for p in pairs]),
        "candidate": _side(agents[1], [p["candidate"] for p in pairs]),
        "summary": {
            "pairs": len(pairs),
            "status_equal": sum(p["differences"]["status_equal"] for p in pairs),
            "output_equal": sum(p["differences"]["output_equal"] for p in pairs),
            "duration_ms": round((time.perf_counter() - started) * 1000, 1),
        },
        "regressions": sorted(p["id"] for p in pairs if p["differences"]["regressed"]),
        "fixed": sorted(p["id"] for p in pairs if p["differences"]["fixed"]),
        "gates": verdict,
        "promotion_eligible": all(verdict.values()),
        "pairs": pairs,
        "limitations": "Both versions ran the same inputs at the same time, each with its own "
        "RunWeave and budgets. Outputs are compared but never stored. Eligibility is advice: "
        "traffic is not switched to the candidate.",
    }
    folder = project.eval.reports / str(report["id"])
    folder.mkdir(parents=True, exist_ok=False)
    (folder / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    (folder / "report.md").write_text(markdown(report), encoding="utf-8")
    return report, folder


def _label(side: dict[str, Any]) -> str:
    return f"{side['agent']['name']} {side['agent']['version']}"


def markdown(report: dict[str, Any]) -> str:
    primary, candidate = report["primary"], report["candidate"]
    eligible = report["promotion_eligible"]
    lines = [
        f"# Shadow: {_label(candidate)} against {_label(primary)} on {report['dataset']['name']}",
        "",
        f"**{'Eligible for promotion' if eligible else 'Not eligible'}** "
        f"({report['environment']['mode']}).",
        "",
        "| Gate | Result |",
        "| --- | --- |",
        *(f"| {name} | {'pass' if ok else 'FAIL'} |" for name, ok in report["gates"].items()),
        "",
        "| | Current | Candidate |",
        "| --- | ---: | ---: |",
        f"| Pass rate | {primary['pass_rate']:.0%} | {candidate['pass_rate']:.0%} |",
        f"| p50 / p95 latency ms | {primary['p50_latency_ms']} / {primary['p95_latency_ms']} "
        f"| {candidate['p50_latency_ms']} / {candidate['p95_latency_ms']} |",
        f"| Tokens | {primary['tokens']} | {candidate['tokens']} |",
        f"| Estimated cost | ${primary['cost_usd']} | ${candidate['cost_usd']} |",
        "",
    ]
    changed = [
        p for p in report["pairs"] if p["differences"]["regressed"] or p["differences"]["fixed"]
    ]
    if changed:
        lines += ["| Case | Change | Assertions now failing |", "| --- | --- | --- |"]
        for item in changed:
            d = item["differences"]
            lines.append(
                f"| {item['id']} | {'regressed' if d['regressed'] else 'fixed'} "
                f"| {', '.join(d['newly_failing']) or '-'} |"
            )
        lines.append("")
    lines += [report["limitations"], ""]
    return "\n".join(lines)


def summary_text(report: dict[str, Any], folder: Path) -> str:
    primary, candidate = report["primary"], report["candidate"]
    lines = [
        f"{_label(candidate)} against {_label(primary)} on {report['dataset']['name']}: "
        f"{candidate['pass_rate']:.0%} vs {primary['pass_rate']:.0%} passed, "
        f"p95 {candidate['p95_latency_ms']} vs {primary['p95_latency_ms']} ms, "
        f"${candidate['cost_usd']} vs ${primary['cost_usd']}"
    ]
    for item in report["pairs"]:
        d = item["differences"]
        if d["regressed"]:
            lines.append(f"  REGRESSED {item['id']}: {', '.join(d['newly_failing']) or 'failed'}")
        elif d["fixed"]:
            lines.append(f"  fixed {item['id']}")
    failed = [name for name, ok in report["gates"].items() if not ok]
    lines.append(
        "Eligible for promotion (nothing is switched automatically)."
        if report["promotion_eligible"]
        else "Not eligible: " + ", ".join(failed)
    )
    lines.append(f"Report: {folder}")
    return "\n".join(lines)
