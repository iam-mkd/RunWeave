"""Does watching a load test in the workspace slow the run being measured?

    uv run --no-sync python scripts/measure-ui-overhead.py --output target/ui-overhead.json

In a fresh `runweave init` project, with the workspace running, it alternates the same
`runweave bench` from a terminal ("cli") and from the Load tests screen's API ("ui"),
while a client polls the workspace exactly as the open screen does (the job's events
every second, plus the project, report and job lists on load). It compares each level's
status-API p95, run latency p95 and throughput across rounds (medians). The poller stands
in for the browser: the screen's charts render in the browser process, not the server.
"""

import argparse
import json
import os
import platform
import socket
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
IO_AGENT = """import asyncio

from pydantic import BaseModel, Field

from runweave.sdk import RunContext, agent


class WordCountInput(BaseModel):
    text: str = Field(min_length=1, max_length=5000)


class WordCount(BaseModel):
    words: int
    longest: str


@agent(name="word-count", version="0.1.0", input=WordCountInput, output=WordCount)
async def run(input: WordCountInput, ctx: RunContext) -> WordCount:
    async with ctx.step("Wait like a model call"):
        await asyncio.sleep(WAIT_SECONDS)
    words = input.text.split()
    return WordCount(words=len(words), longest=max(words, key=len))
"""


def env() -> dict[str, str]:
    values = {k: v for k, v in os.environ.items() if not k.startswith("RUNWEAVE_")}
    values["PYTHONPATH"] = str(ROOT / "src")
    return values


def latest_bench(project: Path) -> dict[str, Any]:
    folder = sorted((project / ".runweave" / "reports").glob("bench-*"))[-1]
    data: dict[str, Any] = json.loads((folder / "report.json").read_text(encoding="utf-8"))
    return data


def screen_poller(client: httpx.Client, job_id: str, stop: threading.Event) -> int:
    client.get("/v1/jobs/project")
    client.get("/v1/reports", params={"kind": "bench"})
    client.get("/v1/jobs")
    after, requests = 0, 3
    while not stop.is_set():
        body = client.get(f"/v1/jobs/{job_id}", params={"after": after}).json()
        requests += 1
        if body["events"]:
            after = body["events"][-1]["seq"]
        if body["job"]["status"] != "RUNNING":
            break
        stop.wait(1.0)
    return requests


def run_cli(project: Path, bench_args: list[str]) -> dict[str, Any]:
    subprocess.run(
        [sys.executable, "-m", "runweave.cli", "bench", *bench_args],
        cwd=project,
        env=env(),
        check=True,
        capture_output=True,
    )
    return latest_bench(project)


def run_ui(client: httpx.Client, project: Path, levels: list[int], runs: int) -> dict[str, Any]:
    started = client.post(
        "/v1/jobs",
        json={"kind": "bench", "concurrency": levels, "runs": runs, "fault_profiles": [""]},
    )
    started.raise_for_status()
    job_id = started.json()["id"]
    stop = threading.Event()
    polls = screen_poller(client, job_id, stop)
    job = client.get(f"/v1/jobs/{job_id}").json()["job"]
    if job["status"] != "COMPLETED":
        raise RuntimeError(f"UI job ended {job}")
    report = latest_bench(project)
    report["_screen_requests"] = polls
    return report


def median(values: list[float | None]) -> float | None:
    present = [v for v in values if v is not None]
    return round(statistics.median(present), 2) if present else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--concurrency", type=int, nargs="+", default=[8, 64])
    parser.add_argument("--runs", type=int, default=300)
    parser.add_argument("--rounds", type=int, default=5)
    parser.add_argument("--wait-ms", type=float, default=50, help="Simulated model wait per run")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    bench_args = ["--concurrency", *map(str, args.concurrency), "--runs", str(args.runs)]
    with tempfile.TemporaryDirectory(prefix="runweave-ui-", ignore_cleanup_errors=True) as scratch:
        work = Path(scratch)
        subprocess.run(
            [sys.executable, "-m", "runweave.cli", "init", "proj"], cwd=work, env=env(), check=True
        )
        project = work / "proj"
        # Wait like a model call would, so the run is I/O-bound (RunWeave's target workload).
        (project / "agent.py").write_text(
            IO_AGENT.replace("WAIT_SECONDS", str(args.wait_ms / 1000)), encoding="utf-8"
        )
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        server = subprocess.Popen(
            [
                sys.executable, "-m", "runweave.cli", "serve", "--port", str(port),
                "--assets", str(ROOT / "frontend" / "dist"), "--agent", "agent:run",
            ],
            cwd=project,
            env=env(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )  # fmt: skip
        try:
            with httpx.Client(
                base_url=f"http://127.0.0.1:{port}", timeout=60, trust_env=False
            ) as client:
                for _ in range(300):
                    try:
                        if client.get("/health/ready").status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    time.sleep(0.2)
                samples: dict[str, list[dict[str, Any]]] = {"cli": [], "ui": []}
                for round_number in range(args.rounds):
                    for mode in ("cli", "ui") if round_number % 2 == 0 else ("ui", "cli"):
                        report = (
                            run_cli(project, bench_args)
                            if mode == "cli"
                            else run_ui(client, project, args.concurrency, args.runs)
                        )
                        samples[mode].append(report)
                        print(f"round {round_number + 1} {mode}: {report['id']}", flush=True)
        finally:
            server.terminate()
            server.wait(timeout=20)
    levels = []
    for index, level in enumerate(args.concurrency):
        row: dict[str, Any] = {"concurrency": level}
        for mode, reports in samples.items():
            cases = [r["cases"][index] for r in reports]
            row[mode] = {
                "status_api_p95_ms": median([c["status_api_ms"]["p95"] for c in cases]),
                "latency_p95_ms": median([c["latency_ms"]["p95"] for c in cases]),
                "throughput_rps": median([c["throughput_rps"] for c in cases]),
                "status_api_samples": median([c["status_api_samples"] for c in cases]),
                "rounds_status_api_p95_ms": [c["status_api_ms"]["p95"] for c in cases],
            }
        cli, ui = row["cli"], row["ui"]
        row["status_api_p95_change_ms"] = round(
            ui["status_api_p95_ms"] - cli["status_api_p95_ms"], 2
        )
        row["within_noise"] = (
            ui["status_api_p95_ms"] <= cli["status_api_p95_ms"] * 1.2 + 1
            and ui["throughput_rps"] >= cli["throughput_rps"] * 0.9
        )
        levels.append(row)
    result = {
        "kind": "ui-overhead",
        "measured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "environment": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "logical_cpus": os.cpu_count(),
        },
        "workload": {
            "agent": f"runweave init word-count with a {args.wait_ms:g} ms await per run",
            "concurrency": args.concurrency,
            "runs_per_level": args.runs,
            "rounds": args.rounds,
            "order": "alternating cli/ui, medians across rounds",
            "screen": "the Load tests screen's requests: job events every second, plus "
            "project, report and job lists on load",
        },
        "screen_requests_per_ui_run": median([r["_screen_requests"] for r in samples["ui"]]),
        "levels": levels,
        "no_measurable_regression": all(row["within_noise"] for row in levels),
        "threshold": "UI status-API p95 at most 1.2x the CLI's + 1 ms, and throughput at "
        "least 90% of the CLI's",
    }
    rendered = json.dumps(result, indent=2)
    if args.output:
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if result["no_measurable_regression"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
