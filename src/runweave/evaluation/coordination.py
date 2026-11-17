"""Paired offline quality, usage, and illustrative latency comparison."""

import argparse
import asyncio
import json
import os
import platform
import sys
from pathlib import Path

from runweave.budgets import BudgetPolicy, BudgetState, MemoryBudget, budget_scope
from runweave.evaluation.suite import (
    Workflow,
    check_observation,
    execute_fixture,
    load_fixtures,
    source_hashes,
)


async def compare() -> dict[str, object]:
    fixtures, digest = await asyncio.to_thread(load_fixtures)
    workflows: tuple[Workflow, Workflow] = ("baseline", "coordinated")
    hashes = {workflow: await asyncio.to_thread(source_hashes, workflow) for workflow in workflows}
    policy = BudgetPolicy(
        price_version="synthetic-comparison-v1",
        input_nanos_per_token=1000,
        output_nanos_per_token=2000,
    )
    pairs = []
    passed = True
    for fixture in fixtures.cases:
        if fixture.name not in ("healthy", "degraded"):
            continue
        measurements: dict[str, object] = {}
        for workflow in ("baseline", "coordinated"):
            ledger = MemoryBudget(BudgetState(policy=policy))
            started = asyncio.get_running_loop().time()
            with budget_scope(ledger):
                output = await execute_fixture(fixture, workflow, tool_delay=0.02)
            elapsed = asyncio.get_running_loop().time() - started
            checks = check_observation(fixture, output)
            passed = passed and all(checks.values())
            measurements[workflow] = {
                "quality": checks,
                "usage": ledger.state.summary().model_dump(mode="json"),
                "elapsed_seconds": elapsed,
                "severity": output.result.severity if output.result else None,
            }
        pairs.append({"case": fixture.name, "measurements": measurements})
    return {
        "schema_version": "coordination-comparison-v1",
        "source_hashes": hashes,
        "budget_policy": policy.model_dump(mode="json"),
        "passed": passed and len(pairs) == 2,
        "fixture_sha256": digest,
        "model": "fake-v1",
        "prices": "synthetic, not provider billing",
        "input_usd_per_million": 1,
        "output_usd_per_million": 2,
        "input_estimator": "conservative UTF-8 bytes",
        "tool_delay_seconds": 0.02,
        "coordination_concurrency": 2,
        "workload": "paired healthy/degraded fixtures, one job at a time",
        "host": {
            "platform": platform.platform(),
            "machine": platform.machine(),
            "cpu_count": os.cpu_count(),
            "python": sys.version,
        },
        "pairs": pairs,
        "limitations": "Two illustrative latency samples per workflow; "
        "fake model and synthetic prices. "
        "Not a provider quality, production capacity, or billing measurement.",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = asyncio.run(compare())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
