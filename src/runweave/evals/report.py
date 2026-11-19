"""Eval reports: JSON (the source of truth, read back by the workspace), JUnit XML and a
readable Markdown summary, plus baseline comparison and pass/fail gates."""

import hashlib
import json
import math
import xml.etree.ElementTree as ET
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

REPORT_VERSION = 1

Progress = Callable[[dict[str, Any]], None]
"""Receives progress events (`{"event": "case", ...}`); `--progress` prints them as JSON lines."""


def silent(event: dict[str, Any]) -> None:
    """The default progress sink: report nothing."""


def new_id(kind: str) -> str:
    return f"{kind}-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{uuid4().hex[:6]}"


def file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(fraction * len(ordered)) - 1)
    return round(ordered[index], 1)


def summarize(cases: list[dict[str, Any]], duration_ms: float) -> dict[str, Any]:
    latencies = [c["latency_ms"] for c in cases if c["status"] != "REJECTED"]
    passed = sum(c["passed"] for c in cases)
    cost = sum((Decimal(str(c["cost_usd"])) for c in cases), Decimal(0))
    return {
        "total": len(cases),
        "passed": passed,
        "failed": len(cases) - passed,
        "pass_rate": round(passed / len(cases), 4) if cases else 0.0,
        "duration_ms": round(duration_ms, 1),
        "p50_latency_ms": percentile(latencies, 0.5),
        "p95_latency_ms": percentile(latencies, 0.95),
        "tokens": sum(c["tokens"] for c in cases),
        "cost_usd": str(cost),
    }


def compare(
    report: dict[str, Any], baseline: dict[str, Any] | None, path: Path | None
) -> dict[str, Any] | None:
    if baseline is None:
        return None
    before = {c["id"]: c["passed"] for c in baseline.get("cases", [])}
    now = {c["id"]: c["passed"] for c in report["cases"]}
    return {
        "path": str(path) if path else None,
        "report_id": baseline.get("id"),
        "pass_rate": baseline.get("summary", {}).get("pass_rate"),
        "regressions": sorted(i for i, ok in now.items() if before.get(i) is True and not ok),
        "fixed": sorted(i for i, ok in now.items() if before.get(i) is False and ok),
        "new": sorted(i for i in now if i not in before),
        "removed": sorted(i for i in before if i not in now),
    }


def gates(report: dict[str, Any], min_pass_rate: float) -> dict[str, Any]:
    reasons = []
    rate = report["summary"]["pass_rate"]
    if rate < min_pass_rate:
        reasons.append(f"pass rate {rate:.0%} is below the minimum {min_pass_rate:.0%}")
    baseline = report.get("baseline")
    if baseline and baseline["regressions"]:
        reasons.append(f"{len(baseline['regressions'])} case(s) regressed against the baseline")
    return {"min_pass_rate": min_pass_rate, "passed": not reasons, "reasons": reasons}


def junit(report: dict[str, Any]) -> bytes:
    summary = report["summary"]
    suites = ET.Element(
        "testsuites",
        name=f"runweave eval {report['dataset']['name']}",
        tests=str(summary["total"]),
        failures=str(summary["failed"]),
        time=f"{summary['duration_ms'] / 1000:.3f}",
    )
    suite = ET.SubElement(
        suites,
        "testsuite",
        name=report["dataset"]["name"],
        tests=str(summary["total"]),
        failures=str(summary["failed"]),
        time=f"{summary['duration_ms'] / 1000:.3f}",
    )
    for case in report["cases"]:
        element = ET.SubElement(
            suite,
            "testcase",
            classname=f"{report['agent']['name']}",
            name=case["id"],
            time=f"{case['latency_ms'] / 1000:.3f}",
        )
        failed = [a for a in case["assertions"] if not a["passed"]]
        if failed:
            failure = ET.SubElement(
                element, "failure", message="; ".join(a["name"] for a in failed)
            )
            failure.text = "\n".join(f"{a['name']}: {a['message']}" for a in failed)
    data: bytes = ET.tostring(suites, encoding="utf-8", xml_declaration=True)
    return data


def markdown(report: dict[str, Any]) -> str:
    summary, verdict = report["summary"], report["gates"]
    lines = [
        f"# Eval: {report['agent']['name']} on {report['dataset']['name']}",
        "",
        f"**{'Passed' if verdict['passed'] else 'Failed'}**: {summary['passed']} of "
        f"{summary['total']} cases passed ({summary['pass_rate']:.0%}).",
    ]
    lines += [f"- {reason}" for reason in verdict["reasons"]]
    lines += [
        "",
        "| Metric | Value |",
        "| --- | --- |",
        "| Agent | {name} {version} ({kind}) |".format(**report["agent"]),
        f"| Mode | {report['environment']['mode']} |",
        f"| p50 / p95 latency | {summary['p50_latency_ms']} / {summary['p95_latency_ms']} ms |",
        f"| Tokens | {summary['tokens']} |",
        f"| Estimated cost | ${summary['cost_usd']} |",
        f"| Report | {report['id']} |",
        "",
    ]
    failed = [c for c in report["cases"] if not c["passed"]]
    if failed:
        lines += ["## Failed cases", "", "| Case | Assertion | Why |", "| --- | --- | --- |"]
        for case in failed:
            for item in (a for a in case["assertions"] if not a["passed"]):
                why = item["message"].replace("|", "\\|")
                lines.append(f"| {case['id']} | {item['name']} | {why} |")
        lines.append("")
    if report.get("baseline"):
        b = report["baseline"]
        lines.append(
            f"Baseline {b['report_id']}: {len(b['regressions'])} regressed, "
            f"{len(b['fixed'])} fixed, {len(b['new'])} new."
        )
    return "\n".join(lines) + "\n"


def write(report: dict[str, Any], folder: Path) -> Path:
    target: Path = folder / str(report["id"])
    target.mkdir(parents=True, exist_ok=False)
    (target / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    (target / "junit.xml").write_bytes(junit(report))
    (target / "report.md").write_text(markdown(report), encoding="utf-8")
    return target
