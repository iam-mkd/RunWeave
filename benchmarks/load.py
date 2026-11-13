import argparse
import asyncio
import json
import os
import platform
from collections import Counter, deque
from collections.abc import Iterable
from datetime import datetime
from math import ceil
from pathlib import Path
from time import monotonic
from typing import cast

import httpx
import psutil  # type: ignore[import-untyped]

from benchmarks.mock_health import MockHealthService
from runweave.app import create_app
from runweave.core.runtime import Runtime
from runweave.core.settings import Settings


def percentiles(samples: Iterable[float]) -> dict[str, float]:
    ordered = sorted(samples)
    return (
        {f"p{p}": round(ordered[ceil(len(ordered) * p / 100) - 1] * 1000, 3) for p in (50, 95, 99)}
        if ordered
        else {}
    )


def metric_samples(body: str) -> dict[str, float]:
    samples = {}
    for line in body.splitlines():
        if line and not line.startswith("#"):
            name, value = line.rsplit(" ", 1)
            samples[name] = float(value)
    return samples


def lag_p99_upper_ms(samples: dict[str, float]) -> float | None:
    name = "runweave_event_loop_lag_seconds"
    count = samples[f"{name}_count"]
    if count == 0:
        return None
    for key, value in samples.items():
        if key.startswith(f'{name}_bucket{{le="') and value >= ceil(count * 0.99):
            bound = key.split('"')[1]
            return None if bound == "+Inf" else 1000 * float(bound)
    return None


async def submit(client: httpx.AsyncClient, service: str = "payments") -> str:
    response = await client.post(
        "/v1/runs", json={"service": service, "description": "Synthetic latency spike"}
    )
    if response.status_code != 202:
        raise RuntimeError(f"Unexpected admission status {response.status_code}")
    return response.headers["Location"]


def config(workers: int, runs: int, *, faults: bool = False) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]  # BaseSettings dotenv override
        model_provider="fake",
        worker_count=workers,
        queue_capacity=runs,
        run_timeout_seconds=180,
        step_timeout_seconds=0.5 if faults else 5,
        history_max_records=runs,
        mock_runbook_delay_seconds=0.2,
        json_logs=False,
        console_traces=False,
        http_max_connections=128,
        history_ttl_seconds=3600,
        agent_max_steps=7,
        agent_completion_tokens=2048,
        tool_retry_attempts=3,
        tool_retry_base_delay_seconds=0.1,
        tool_retry_max_delay_seconds=2,
    )


async def workload(workers: int, runs: int, soak_seconds: float = 0) -> dict[str, object]:
    app = create_app(
        config(workers, runs), mock_transport=httpx.MockTransport(MockHealthService(0.19).respond)
    )
    process = psutil.Process()
    rss_start = process.memory_info().rss
    rss_peak = rss_start
    status_latency: deque[float] = deque(maxlen=8192)
    outcomes: Counter[str] = Counter()
    durations: deque[float] = deque(maxlen=8192)
    queue_wait: deque[float] = deque(maxlen=8192)
    rss_samples: list[dict[str, float]] = []
    batches = 0
    started = monotonic()
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://runweave.test"
        ) as client,
    ):
        runtime = cast(Runtime, app.state.runtime)
        while batches == 0 or monotonic() - started < soak_seconds:
            locations = [await submit(client) for _ in range(runs)]
            pending = asyncio.create_task(runtime.scheduler.join())
            try:
                async with asyncio.timeout(185):
                    while not pending.done():
                        probe = monotonic()
                        (await client.get(locations[-1])).raise_for_status()
                        status_latency.append(monotonic() - probe)
                        rss_peak = max(rss_peak, process.memory_info().rss)
                        await asyncio.wait({pending}, timeout=0.05)
                await pending
            finally:
                if not pending.done():
                    pending.cancel()
                    await asyncio.gather(pending, return_exceptions=True)
            for location in locations:
                result = (await client.get(location)).raise_for_status().json()
                outcomes[result["status"]] += 1
                admitted = datetime.fromisoformat(result["submitted_at"])
                durations.append(
                    (datetime.fromisoformat(result["finished_at"]) - admitted).total_seconds()
                )
                queue_wait.append(
                    (datetime.fromisoformat(result["started_at"]) - admitted).total_seconds()
                )
            batches += 1
            sample_time = monotonic() - started
            if not rss_samples or sample_time - rss_samples[-1]["elapsed_seconds"] >= 30:
                rss_samples.append(
                    {
                        "elapsed_seconds": round(sample_time, 3),
                        "rss_mib": round(process.memory_info().rss / 2**20, 2),
                    }
                )
        elapsed = monotonic() - started
        metrics = metric_samples((await client.get("/metrics")).raise_for_status().text)

    peak = int(metrics["runweave_peak_active_runs"])
    peak_tools = int(metrics["runweave_peak_active_tool_steps"])
    assert outcomes == {"SUCCEEDED": runs * batches}, outcomes
    assert peak <= workers and peak_tools <= workers
    if workers >= 100 and runs >= 100:
        assert peak >= 100 and peak_tools >= 100, (peak, peak_tools)
    assert metrics["runweave_queue_depth"] == metrics["runweave_active_runs"] == 0
    assert metrics["runweave_retained_runs"] <= runs
    assert not any(task.get_name().startswith("runweave-") for task in asyncio.all_tasks())
    return {
        "workers": workers,
        "requests": runs * batches,
        "batches": batches,
        "duration_seconds": round(elapsed, 3),
        "runs_per_second": round(runs * batches / elapsed, 2),
        "outcomes": outcomes,
        "peak_active_runs": peak,
        "peak_active_tool_steps": peak_tools,
        "completion_ms": percentiles(durations),
        "queue_wait_ms": percentiles(queue_wait),
        "status_api_ms": percentiles(status_latency),
        "status_api_samples": len(status_latency),
        "completion_samples": len(durations),
        "loop_lag_p99_bucket_ms": lag_p99_upper_ms(metrics),
        "rss_start_mib": round(rss_start / 2**20, 2),
        "rss_peak_mib": round(rss_peak / 2**20, 2),
        "rss_end_mib": round(process.memory_info().rss / 2**20, 2),
        "rss_samples": rss_samples,
        "retained_runs": int(metrics["runweave_retained_runs"]),
        "retries": int(metrics["runweave_tool_retries_total"]),
    }


async def failure_matrix() -> dict[str, object]:
    expected = {
        "rate-limited": "SUCCEEDED",
        "transient": "SUCCEEDED",
        "disconnect": "SUCCEEDED",
        "forbidden": "FAILED",
        "malformed": "FAILED",
        "slow": "TIMED_OUT",
        "cancelled": "CANCELLED",
    }
    mock = MockHealthService(0.05)
    app = create_app(config(8, 16, faults=True), mock_transport=httpx.MockTransport(mock.respond))
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://runweave.test"
        ) as client,
    ):
        runtime = cast(Runtime, app.state.runtime)
        locations = {service: await submit(client, service) for service in expected}
        async with asyncio.timeout(2):
            await mock.cancel_target_started.wait()
            cancelled = await client.post(f"{locations['cancelled']}/cancel")
            assert cancelled.json()["status"] == "CANCELLED"
            await runtime.scheduler.join()
        actual = {
            service: (await client.get(url)).json()["status"] for service, url in locations.items()
        }
        metrics = metric_samples((await client.get("/metrics")).text)
        assert actual == expected, actual
        assert metrics["runweave_tool_retries_total"] == 3
        return {"outcomes": actual, "retried_services": mock.attempts, "retries": 3}


async def run(args: argparse.Namespace) -> None:
    report = {
        "environment": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "logical_cpus": os.cpu_count(),
            "cpu": platform.processor(),
            "memory_mib": round(psutil.virtual_memory().total / 2**20),
            "processes": 1,
            "event_loop": args.environment_note,
        },
        "workload": "HTTPX ASGI transport, fake model, two 200 ms synthetic tool waits",
        "queue_capacity": args.runs,
        "http_max_connections": 128,
        "cases": [
            await workload(workers, args.runs, args.soak_seconds) for workers in args.workers
        ],
        "faults": await failure_matrix(),
    }
    rendered = json.dumps(report, indent=2)
    if args.output:
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run offline RunWeave load and recovery checks")
    parser.add_argument("--workers", type=int, nargs="+", default=[1, 10, 50, 100, 128])
    parser.add_argument("--runs", type=int, default=128)
    parser.add_argument("--soak-seconds", type=float, default=0)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--environment-note", default="Native asyncio")
    args = parser.parse_args()
    if not 1 <= args.runs <= 256 or any(not 1 <= w <= 128 for w in args.workers):
        parser.error("Use 1–256 runs and 1–128 workers")
    if not 0 <= args.soak_seconds <= 600:
        parser.error("Use a soak duration between 0 and 600 seconds")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
