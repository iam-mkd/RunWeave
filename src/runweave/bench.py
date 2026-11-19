"""`runweave bench`: load-test the project's agent and report what it costs to run it.

Each fault profile gets its own in-process RunWeave (or the `--server` instance and its own
fault settings). Within it, every concurrency level submits the same number of runs with at
most that many in flight, using the dataset's inputs in turn. Latency and queue wait come
from the server's own timestamps, so the driver's polling does not inflate them. A probe
measures the status API and memory while the load runs, and `/metrics` gives event-loop lag
for the window. An optional soak keeps the highest level busy for a while and records
throughput, latency and memory over time.
"""

import asyncio
import json
import os
import platform
import time
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from math import ceil
from pathlib import Path
from typing import Any

import httpx
import psutil  # type: ignore[import-untyped]

from runweave.evals import report as reports
from runweave.evals.cases import load_cases
from runweave.evals.runner import TERMINAL, InProcessLimits, Target, environment, open_target
from runweave.project import Project, ProjectError

LAG_METRIC = "runweave_event_loop_lag_seconds"
PROBE_INTERVAL_SECONDS = 0.05
TICK_SECONDS = 0.5
MAX_ADMISSION_ATTEMPTS = 200


@dataclass(frozen=True)
class BenchOptions:
    dataset: str | None = None
    server: str | None = None
    concurrency: tuple[int, ...] | None = None
    runs: int | None = None
    fault_profiles: tuple[str, ...] | None = None
    soak_seconds: float | None = None
    progress: reports.Progress = reports.silent


def percentiles(values: Iterable[float]) -> dict[str, float | None]:
    ordered = sorted(values)
    if not ordered:
        return {"p50": None, "p95": None, "p99": None}
    return {
        f"p{p}": round(ordered[max(0, ceil(len(ordered) * p / 100) - 1)], 1) for p in (50, 95, 99)
    }


def parse_metrics(text: str) -> dict[str, float]:
    samples: dict[str, float] = {}
    for line in text.splitlines():
        if line and not line.startswith("#"):
            name, _, value = line.rpartition(" ")
            try:
                samples[name] = float(value)
            except ValueError:
                continue
    return samples


def lag_p99_upper_ms(before: dict[str, float], after: dict[str, float]) -> float | None:
    """Upper bound of the histogram bucket holding the window's 99th-percentile loop lag."""
    count = after.get(f"{LAG_METRIC}_count", 0) - before.get(f"{LAG_METRIC}_count", 0)
    if count <= 0:
        return None
    prefix = f'{LAG_METRIC}_bucket{{le="'
    buckets = sorted(
        (float(key[len(prefix) : -2]), value - before.get(key, 0))
        for key, value in after.items()
        if key.startswith(prefix)
    )
    for bound, cumulative in buckets:
        if cumulative >= ceil(count * 0.99):
            return None if bound == float("inf") else round(bound * 1000, 3)
    return None


def failure_category(snapshot: dict[str, Any]) -> str | None:
    status = str(snapshot.get("status"))
    if status == "SUCCEEDED":
        return None
    error = snapshot.get("error") or {}
    code = error.get("code") if isinstance(error, dict) else None
    return f"{status}:{code}" if code else status


@dataclass(frozen=True)
class Sample:
    status: str
    category: str | None
    submitted: datetime | None
    finished: datetime | None
    queue_wait_ms: float | None
    tokens: int
    cost: Decimal
    model_calls: int
    admission_retries: int
    observed_at: float = field(default_factory=time.monotonic)

    @property
    def latency_ms(self) -> float | None:
        if self.submitted is None or self.finished is None:
            return None
        return max(0.0, (self.finished - self.submitted).total_seconds() * 1000)


def _time(value: Any) -> datetime | None:
    return datetime.fromisoformat(value) if isinstance(value, str) else None


def _sample(snapshot: dict[str, Any], retries: int) -> Sample:
    submitted, started = _time(snapshot.get("submitted_at")), _time(snapshot.get("started_at"))
    usage = snapshot.get("usage") or {}
    return Sample(
        status=str(snapshot["status"]),
        category=failure_category(snapshot),
        submitted=submitted,
        finished=_time(snapshot.get("finished_at")),
        queue_wait_ms=(started - submitted).total_seconds() * 1000
        if started and submitted
        else None,
        tokens=int(usage.get("input_tokens") or 0) + int(usage.get("output_tokens") or 0),
        cost=Decimal(str(usage.get("estimated_cost_usd") or 0)),
        model_calls=sum(s.get("kind") == "model_call" for s in snapshot.get("steps") or []),
        admission_retries=retries,
    )


class Driver:
    """Submits runs and waits for them, and probes the status API while they run."""

    def __init__(
        self,
        target: Target,
        inputs: list[dict[str, Any]],
        measure_memory: bool,
        progress: reports.Progress = reports.silent,
    ):
        self.target = target
        self.inputs = inputs
        self.process = psutil.Process() if measure_memory else None
        self.latest_url: str | None = None
        self.next_input = 0
        self.progress = progress
        self.started = time.monotonic()

    def payload(self) -> dict[str, Any]:
        payload = self.inputs[self.next_input % len(self.inputs)]
        self.next_input += 1
        return payload

    async def one(self) -> Sample:
        client = self.target.client
        retries = 0
        for _ in range(MAX_ADMISSION_ATTEMPTS):
            response = await client.post(
                "/v1/runs", json={"agent": self.target.agent["name"], "input": self.payload()}
            )
            if response.status_code != 503:
                break
            retries += 1
            await asyncio.sleep(min(1.0, float(response.headers.get("retry-after") or 0.2)))
        if response.status_code != 202:
            try:
                detail = response.json().get("detail")
            except ValueError:
                detail = None
            code = detail.get("code") if isinstance(detail, dict) else None
            return _sample(
                {"status": "REJECTED", "error": {"code": code or f"HTTP_{response.status_code}"}},
                retries,
            )
        url = response.json()["status_url"]
        self.latest_url = url
        return _sample(await self.wait(url), retries)

    async def wait(self, url: str) -> dict[str, Any]:
        client = self.target.client
        try:
            async with client.stream("GET", f"{url}/events") as stream:
                if stream.status_code == 200:
                    async for line in stream.aiter_lines():
                        if line.startswith("data: "):
                            snapshot: dict[str, Any] = json.loads(line[6:])
                            if snapshot.get("status") in TERMINAL:
                                return snapshot
        except (httpx.HTTPError, ValueError):
            pass
        delay = 0.02  # streams are busy or ended early: poll instead
        while True:
            polled: dict[str, Any] = (await client.get(url)).json()
            if polled.get("status") in TERMINAL:
                return polled
            await asyncio.sleep(delay)
            delay = min(0.25, delay * 1.5)

    def rss_mib(self) -> float | None:
        return round(self.process.memory_info().rss / 2**20, 2) if self.process else None


@dataclass
class Probe:
    """Measures the status API and peak memory every 50 ms while load runs."""

    driver: Driver
    latencies: list[float] = field(default_factory=list)
    rss_peak: float | None = None

    async def sample(self) -> None:
        if self.driver.latest_url is not None:
            started = time.perf_counter()
            try:
                (await self.driver.target.client.get(self.driver.latest_url)).raise_for_status()
                self.latencies.append((time.perf_counter() - started) * 1000)
            except httpx.HTTPError:
                pass
        rss = self.driver.rss_mib()
        if rss is not None:
            self.rss_peak = max(self.rss_peak or 0, rss)

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            await self.sample()
            # Until the first run is admitted there is nothing to probe; check again soon
            # so a level that finishes within one interval still gets a sample under load.
            interval = PROBE_INTERVAL_SECONDS if self.latencies else 0.002
            try:
                await asyncio.wait_for(stop.wait(), interval)
            except TimeoutError:
                pass
        if not self.latencies:
            await self.sample()  # the level ended before any probe: measure it once now


async def _ticks(
    driver: Driver,
    labels: dict[str, Any],
    samples: list[Sample],
    stop: asyncio.Event,
    runs: int | None,
) -> None:
    """Live progress for charts: completed runs, recent throughput and p95 so far."""
    if driver.progress is reports.silent:
        return
    last, last_time = 0, time.monotonic()
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), TICK_SECONDS)
        except TimeoutError:
            pass
        now, done = time.monotonic(), len(samples)
        driver.progress(
            {
                "event": "tick",
                **labels,
                "elapsed_ms": round((now - driver.started) * 1000, 1),
                "completed": done,
                "runs": runs,
                "throughput_rps": round((done - last) / max(now - last_time, 1e-6), 2),
                "p95_latency_ms": percentiles(
                    s.latency_ms for s in samples if s.latency_ms is not None
                )["p95"],
                "failures": sum(s.category is not None for s in samples),
            }
        )
        last, last_time = done, now


async def _metrics(client: httpx.AsyncClient) -> dict[str, float] | None:
    try:
        response = await client.get("/metrics")
    except httpx.HTTPError:
        return None
    return parse_metrics(response.text) if response.status_code == 200 else None


def _totals(samples: list[Sample]) -> dict[str, Any]:
    finished = [s for s in samples if s.submitted and s.finished]
    span = (
        (
            max(s.finished for s in finished if s.finished)
            - min(s.submitted for s in finished if s.submitted)
        ).total_seconds()
        * 1000
        if finished
        else 0.0
    )
    return {
        "runs": len(samples),
        "duration_ms": round(span, 1),
        "throughput_rps": round(len(finished) / (span / 1000), 2) if span > 0 else 0.0,
        "latency_ms": percentiles(s.latency_ms for s in samples if s.latency_ms is not None),
        "queue_wait_ms": percentiles(
            s.queue_wait_ms for s in samples if s.queue_wait_ms is not None
        ),
        "outcomes": dict(Counter(s.status for s in samples)),
        "failures": dict(Counter(s.category for s in samples if s.category)),
        "admission_retries": sum(s.admission_retries for s in samples),
        "tokens": sum(s.tokens for s in samples),
        "cost_usd": str(sum((s.cost for s in samples), Decimal(0))),
        "model_calls": sum(s.model_calls for s in samples),
    }


async def measure_level(
    driver: Driver, level: int, runs: int, labels: dict[str, Any] | None = None
) -> dict[str, Any]:
    client = driver.target.client
    before = await _metrics(client)
    rss_start = driver.rss_mib()
    probe = Probe(driver, rss_peak=rss_start)
    stop = asyncio.Event()
    samples: list[Sample] = []
    background = [
        asyncio.create_task(probe.run(stop)),
        asyncio.create_task(_ticks(driver, labels or {}, samples, stop, runs)),
    ]
    slots = asyncio.Semaphore(level)

    async def bounded() -> None:
        async with slots:
            samples.append(await driver.one())

    try:
        await asyncio.gather(*(bounded() for _ in range(runs)))
    finally:
        stop.set()
        await asyncio.gather(*background)
    after = await _metrics(client)
    rss_end = driver.rss_mib()
    return {
        "concurrency": level,
        **_totals(samples),
        "status_api_ms": percentiles(probe.latencies),
        "status_api_samples": len(probe.latencies),
        "loop_lag_p99_bucket_ms": lag_p99_upper_ms(before, after) if before and after else None,
        "peak_active_runs": int(after["runweave_peak_active_runs"])
        if after and "runweave_peak_active_runs" in after
        else None,
        "memory": None
        if rss_start is None or rss_end is None
        else {
            "rss_start_mib": rss_start,
            "rss_peak_mib": max(probe.rss_peak or rss_end, rss_end),
            "rss_end_mib": rss_end,
        },
    }


async def soak(
    driver: Driver, level: int, seconds: float, labels: dict[str, Any] | None = None
) -> dict[str, Any]:
    interval = max(0.5, seconds / 30)
    samples: list[Sample] = []
    windows: list[dict[str, Any]] = []
    started = time.monotonic()
    end = started + seconds
    rss_start = driver.rss_mib()
    rss_peak = rss_start
    stop = asyncio.Event()

    async def worker() -> None:
        while time.monotonic() < end:
            samples.append(await driver.one())

    async def sampler() -> None:
        nonlocal rss_peak
        mark = 0
        while time.monotonic() < end:
            await asyncio.sleep(min(interval, max(0.01, end - time.monotonic())))
            window, mark = samples[mark:], len(samples)
            rss = driver.rss_mib()
            if rss is not None:
                rss_peak = max(rss_peak or 0, rss)
            windows.append(
                {
                    "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
                    "runs": len(window),
                    "throughput_rps": round(len(window) / interval, 2),
                    "p95_latency_ms": percentiles(
                        s.latency_ms for s in window if s.latency_ms is not None
                    )["p95"],
                    "failures": sum(s.category is not None for s in window),
                    "rss_mib": rss,
                }
            )
            driver.progress({"event": "soak_window", **(labels or {}), **windows[-1]})

    ticking = asyncio.create_task(_ticks(driver, labels or {}, samples, stop, None))
    try:
        await asyncio.gather(sampler(), *(worker() for _ in range(level)))
    finally:
        stop.set()
        await ticking
    rss_end = driver.rss_mib()
    totals = _totals(samples)
    totals["duration_ms"] = round((time.monotonic() - started) * 1000, 1)
    totals["throughput_rps"] = round(len(samples) / (totals["duration_ms"] / 1000), 2)
    return {
        "concurrency": level,
        **totals,
        "windows": windows,
        "memory": None
        if rss_start is None or rss_end is None
        else {"rss_start_mib": rss_start, "rss_peak_mib": rss_peak, "rss_end_mib": rss_end},
    }


def _gates(project: Project, measured: list[dict[str, Any]]) -> dict[str, Any]:
    limits = project.bench
    reasons = []
    for case in measured:
        where = f"concurrency {case['concurrency']}"
        if case.get("fault_profile"):
            where += f", fault profile {case['fault_profile']}"
        failed = sum(case["failures"].values())
        rate = failed / case["runs"] if case["runs"] else 0.0
        if limits.max_failure_rate is not None and rate > limits.max_failure_rate:
            reasons.append(
                f"failure rate {rate:.0%} at {where} is above the maximum "
                f"{limits.max_failure_rate:.0%}"
            )
        p95 = case["latency_ms"]["p95"]
        if limits.max_p95_latency_ms is not None and p95 is not None:
            if p95 > limits.max_p95_latency_ms:
                reasons.append(
                    f"p95 latency {p95} ms at {where} is above the maximum "
                    f"{limits.max_p95_latency_ms} ms"
                )
    return {
        "max_failure_rate": limits.max_failure_rate,
        "max_p95_latency_ms": limits.max_p95_latency_ms,
        "passed": not reasons,
        "reasons": reasons,
    }


def _hardware() -> dict[str, Any]:
    return {
        "logical_cpus": os.cpu_count(),
        "cpu": platform.processor() or platform.machine(),
        "memory_mib": round(psutil.virtual_memory().total / 2**20),
    }


async def run_bench(project: Project, options: BenchOptions) -> tuple[dict[str, Any], Path]:
    dataset = project.dataset(options.dataset)
    cases = load_cases(dataset.path)
    inputs = [c.input for c in cases if c.expect.status != "REJECTED"]
    if not inputs:
        raise ProjectError(
            f"Dataset {dataset.name!r} has no inputs to load-test: every case expects a rejection"
        )
    levels = options.concurrency or project.bench.concurrency
    runs = options.runs or project.bench.runs
    soak_seconds = (
        project.bench.soak_seconds if options.soak_seconds is None else options.soak_seconds
    )
    if options.server is not None and options.fault_profiles is not None:
        raise ProjectError(
            "With --server the server's own fault settings apply; change them in its Settings "
            "screen instead of passing --fault-profile"
        )
    profiles: tuple[str | None, ...] = (
        (None,)
        if options.server is not None
        else options.fault_profiles or project.bench.fault_profiles or ("",)
    )
    measured: list[dict[str, Any]] = []
    soaked: dict[str, Any] | None = None
    agent: dict[str, Any] = {}
    mode = "in-process"
    driver: Driver | None = None
    for profile in profiles:
        limits = InProcessLimits(workers=max(levels), fault_profile=profile)
        async with open_target(project, options.server, limits=limits) as target:
            agent, mode = target.agent, target.mode
            if driver is None:
                driver = Driver(target, inputs, options.server is None, options.progress)
            else:
                driver.target, driver.latest_url = target, None
            for level in levels:
                labels = {"fault_profile": profile, "concurrency": level}
                options.progress({"event": "level_started", **labels, "runs": runs})
                result = {
                    "fault_profile": profile,
                    **await measure_level(driver, level, runs, labels),
                }
                options.progress({"event": "level", **result})
                measured.append(result)
            if soak_seconds > 0 and soaked is None:
                labels = {"fault_profile": profile, "concurrency": max(levels), "phase": "soak"}
                options.progress({"event": "soak_started", **labels, "seconds": soak_seconds})
                soaked = {
                    "fault_profile": profile,
                    **await soak(driver, max(levels), soak_seconds, labels),
                }
                options.progress({"event": "soak", **soaked})
    report: dict[str, Any] = {
        "kind": "bench",
        "version": reports.REPORT_VERSION,
        "id": reports.new_id("bench"),
        "created_at": datetime.now(UTC).isoformat(),
        "dataset": {
            "name": dataset.name,
            "path": str(dataset.path.relative_to(project.root)),
            "sha256": reports.file_digest(dataset.path),
            "inputs": len(inputs),
        },
        "agent": {"name": agent["name"], "version": agent["version"], "kind": agent["kind"]},
        "environment": {**environment(mode), **_hardware()},
        "settings": {
            "concurrency": list(levels),
            "runs": runs,
            "fault_profiles": list(profiles),
            "fault_seed": project.proxy.fault_seed,
            "soak_seconds": soak_seconds,
            "run_timeout_seconds": project.limits.run_timeout_seconds,
        },
        "cases": measured,
        "soak": soaked,
        "limitations": (
            "RunWeave, the agent and this load driver share one process and event loop, over "
            "loopback TCP with the mock LLM proxy. Results show how your agent and RunWeave "
            "behave under concurrency and injected faults on this machine, not live-provider "
            "capacity."
            if mode == "in-process"
            else "Load was submitted to a running server over HTTP; memory is not measured "
            "because the server is another process, and peak active runs is the server's "
            "high-water mark since it started."
        ),
    }
    report["gates"] = _gates(project, [*measured, *([soaked] if soaked else [])])
    folder = project.eval.reports / str(report["id"])
    folder.mkdir(parents=True, exist_ok=False)
    (folder / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    (folder / "report.md").write_text(markdown(report), encoding="utf-8")
    return report, folder


def _profile(value: str | None) -> str:
    return "server settings" if value is None else value or "none"


def markdown(report: dict[str, Any]) -> str:
    env = report["environment"]
    lines = [
        f"# Bench: {report['agent']['name']} {report['agent']['version']} "
        f"on {report['dataset']['name']}",
        "",
        f"**{'Passed' if report['gates']['passed'] else 'Failed'}** "
        f"({env['mode']}, {env['logical_cpus']} logical CPUs, {env['memory_mib']} MiB, "
        f"Python {env['python']}, {env['platform']}).",
    ]
    lines += [f"- {reason}" for reason in report["gates"]["reasons"]]
    lines += [
        "",
        "| Faults | Concurrency | Runs | Runs/s | p50 / p95 / p99 ms | Queue wait p95 ms "
        "| Status API p95 ms | Loop lag p99 ms | Peak RSS MiB | Failures | Tokens |",
        "| --- | ---: | ---: | ---: | --- | ---: | ---: | ---: | ---: | --- | ---: |",
    ]
    for case in report["cases"]:
        latency = case["latency_ms"]
        failures = ", ".join(f"{k} {v}" for k, v in case["failures"].items()) or "none"
        memory = case["memory"]["rss_peak_mib"] if case["memory"] else "n/a"
        lines.append(
            f"| {_profile(case['fault_profile'])} | {case['concurrency']} | {case['runs']} "
            f"| {case['throughput_rps']} | {latency['p50']} / {latency['p95']} / {latency['p99']} "
            f"| {case['queue_wait_ms']['p95']} | {case['status_api_ms']['p95']} "
            f"| {case['loop_lag_p99_bucket_ms']} | {memory} | {failures} | {case['tokens']} |"
        )
    if report["soak"]:
        soaked = report["soak"]
        lines += [
            "",
            f"Soak at concurrency {soaked['concurrency']}: {soaked['runs']} runs in "
            f"{soaked['duration_ms'] / 1000:.1f} s ({soaked['throughput_rps']} runs/s), "
            f"p95 {soaked['latency_ms']['p95']} ms, "
            + (
                f"RSS {soaked['memory']['rss_start_mib']} to {soaked['memory']['rss_end_mib']} "
                f"MiB (peak {soaked['memory']['rss_peak_mib']})."
                if soaked["memory"]
                else "memory not measured."
            ),
        ]
    lines += ["", report["limitations"], ""]
    return "\n".join(lines)


def summary_text(report: dict[str, Any], folder: Path) -> str:
    lines = [
        f"{report['agent']['name']} {report['agent']['version']} ({report['environment']['mode']})"
    ]
    for case in report["cases"]:
        failures = sum(case["failures"].values())
        lines.append(
            f"  faults {_profile(case['fault_profile'])}, concurrency {case['concurrency']}: "
            f"{case['throughput_rps']} runs/s, p95 {case['latency_ms']['p95']} ms, "
            f"{failures}/{case['runs']} failed"
        )
    if report["soak"]:
        soaked = report["soak"]
        lines.append(
            f"  soak, concurrency {soaked['concurrency']}: {soaked['runs']} runs, "
            f"{soaked['throughput_rps']} runs/s, p95 {soaked['latency_ms']['p95']} ms"
        )
    verdict = report["gates"]
    lines.append(
        "Gates passed." if verdict["passed"] else "Gates failed: " + "; ".join(verdict["reasons"])
    )
    lines.append(f"Report: {folder}")
    return "\n".join(lines)
