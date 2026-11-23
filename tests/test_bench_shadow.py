"""`runweave bench` and `runweave shadow`: report maths, gates, and the commands end to end.

The commands run in subprocesses, offline, against projects made by `runweave init`, as a
developer would run them.
"""

import asyncio
import json
import os
import socket
import subprocess
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from runweave.bench import (
    SHARD_SLOTS,
    Driver,
    _sample,
    _totals,
    client_shards,
    failure_category,
    lag_p99_upper_ms,
    measure_level,
    parse_metrics,
    percentiles,
)
from runweave.evals.runner import InProcessLimits, open_target
from runweave.evals.shadow import differences, gates
from runweave.project import ShadowSection, load_project

ROOT = Path(__file__).resolve().parents[1]

MODEL_AGENT = '''"""Calls the model once through the RunWeave proxy, so proxy faults reach it."""

import httpx
from pydantic import BaseModel, Field

from runweave.sdk import RunContext, agent


class Ask(BaseModel):
    text: str = Field(min_length=1, max_length=500)


class Answer(BaseModel):
    reply: str


@agent(name="word-count", version="0.2.0", input=Ask, output=Answer, max_steps=5)
async def run(input: Ask, ctx: RunContext) -> Answer:
    assert ctx.proxy is not None
    async with httpx.AsyncClient(base_url=ctx.proxy.openai_base_url, trust_env=False) as client:
        response = await client.post(
            "/chat/completions",
            headers={"Authorization": f"Bearer {ctx.proxy.token}"},
            json={"model": "mock", "messages": [{"role": "user", "content": input.text}]},
        )
        response.raise_for_status()
    return Answer(reply=response.json()["choices"][0]["message"]["content"])
'''


def runweave(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("RUNWEAVE_")}
    env["PYTHONPATH"] = str(ROOT / "src")
    return subprocess.run(
        [sys.executable, "-m", "runweave.cli", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=240,
        check=False,
    )


def latest(project: Path, kind: str) -> dict[str, Any]:
    # By created_at: folder names stamp only the second, then a random suffix.
    reports = [
        (folder, json.loads((folder / "report.json").read_text(encoding="utf-8")))
        for folder in (project / ".runweave" / "reports").glob(f"{kind}-*")
    ]
    assert reports, f"no {kind} report written"
    folder, data = max(reports, key=lambda item: str(item[1]["created_at"]))
    assert (folder / "report.md").is_file()
    return dict(data)


def new_project(tmp_path: Path) -> Path:
    created = runweave("init", "proj", cwd=tmp_path)
    assert created.returncode == 0, created.stderr
    return tmp_path / "proj"


def edit_toml(project: Path, old: str, new: str) -> None:
    path = project / "runweave.toml"
    text = path.read_text(encoding="utf-8")
    assert old in text, old
    path.write_text(text.replace(old, new), encoding="utf-8")


# Report maths


def test_percentiles_use_nearest_rank_and_handle_no_samples() -> None:
    assert percentiles([float(v) for v in range(1, 101)]) == {"p50": 50.0, "p95": 95.0, "p99": 99.0}
    assert percentiles([]) == {"p50": None, "p95": None, "p99": None}


def histogram(count: int, buckets: dict[str, int]) -> str:
    name = "runweave_event_loop_lag_seconds"
    lines = [f'{name}_bucket{{le="{le}"}} {value}' for le, value in buckets.items()]
    return "\n".join([*lines, f"{name}_count {count}", f"{name}_sum 1.0"])


def test_event_loop_lag_is_measured_over_the_window_only() -> None:
    before = parse_metrics(histogram(100, {"0.01": 100, "0.25": 100, "1.0": 100, "+Inf": 100}))
    after = parse_metrics(histogram(110, {"0.01": 100, "0.25": 110, "1.0": 110, "+Inf": 110}))
    assert lag_p99_upper_ms(before, after) == 250.0
    assert lag_p99_upper_ms(after, after) is None
    assert lag_p99_upper_ms({}, after) is not None


def test_failure_categories_name_the_status_and_code() -> None:
    assert failure_category({"status": "SUCCEEDED"}) is None
    assert failure_category({"status": "FAILED", "error": {"code": "RUN_FAILED"}}) == (
        "FAILED:RUN_FAILED"
    )
    assert failure_category({"status": "TIMED_OUT", "error": None}) == "TIMED_OUT"
    assert failure_category({"status": "REJECTED", "error": {"code": "QUEUE_FULL"}}) == (
        "REJECTED:QUEUE_FULL"
    )


def record(case: str, passed: bool, **values: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": case,
        "passed": passed,
        "status": "SUCCEEDED",
        "latency_ms": 10.0,
        "tokens": 10,
        "cost_usd": "0.001",
        "model_calls": 1,
        "assertions": [{"name": "output", "passed": passed, "message": ""}],
    }
    return {**base, **values}


def test_paired_differences() -> None:
    diff = differences(
        record("a", True), record("a", False, latency_ms=25.0, tokens=14), output_equal=False
    )
    assert diff["status_equal"] and not diff["output_equal"]
    assert diff["latency_ms"] == 15.0 and diff["tokens"] == 4
    assert diff["cost_usd"] == "0.000" and diff["model_calls"] == 0
    assert diff["regressed"] and not diff["fixed"] and diff["newly_failing"] == ["output"]


def pair(primary: dict[str, Any], candidate: dict[str, Any], equal: bool = True) -> dict[str, Any]:
    return {
        "id": primary["id"],
        "primary": primary,
        "candidate": candidate,
        "differences": differences(primary, candidate, output_equal=equal),
    }


def test_shadow_gates_follow_the_milestone_16_policy() -> None:
    policy = ShadowSection()
    good = [pair(record(f"c{i}", True), record(f"c{i}", True)) for i in range(4)]
    verdict = gates(good, policy, min_pass_rate=1.0)
    assert verdict == {
        "candidate_pass_rate_met": True,
        "no_paired_regressions": True,
        "outcome_parity": True,
        "candidate_cost_within_limit": True,
        "candidate_latency_within_limit": True,
    }
    regressed = [*good[:3], pair(record("c3", True), record("c3", False))]
    assert not gates(regressed, policy, min_pass_rate=0.5)["no_paired_regressions"]
    assert gates(regressed, policy, min_pass_rate=0.5)["candidate_pass_rate_met"]
    failed = [*good[:3], pair(record("c3", True), record("c3", True, status="FAILED"))]
    assert not gates(failed, policy, min_pass_rate=0)["outcome_parity"]
    pricey = [pair(record("c", True), record("c", True, cost_usd=str(Decimal("0.00106"))))]
    assert not gates(pricey, policy, min_pass_rate=1)["candidate_cost_within_limit"]
    slow = [pair(record("c", True), record("c", True, latency_ms=70.1))]
    assert not gates(slow, policy, min_pass_rate=1)["candidate_latency_within_limit"]
    assert gates([pair(record("c", True), record("c", True, latency_ms=70.0))], policy, 1)[
        "candidate_latency_within_limit"
    ]
    different = [pair(record("c", True), record("c", True), equal=False)]
    assert gates(different, policy, min_pass_rate=1)["outcome_parity"]
    strict = ShadowSection(require_output_parity=True)
    assert not gates(different, strict, min_pass_rate=1)["outcome_parity"]


# `runweave bench`


def test_bench_sweeps_concurrency_and_reports_load_evidence(tmp_path: Path) -> None:
    project = new_project(tmp_path)
    result = runweave("bench", "--concurrency", "4", "1", "--runs", "8", cwd=project)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "runs/s" in result.stdout
    data = latest(project, "bench")
    assert data["kind"] == "bench" and data["agent"]["name"] == "word-count"
    assert data["environment"]["mode"] == "in-process"
    assert {"logical_cpus", "memory_mib", "platform", "python"} <= set(data["environment"])
    assert data["settings"]["concurrency"] == [1, 4] and data["settings"]["runs"] == 8
    assert data["dataset"]["inputs"] == 3  # the expected rejection is not load
    assert [c["concurrency"] for c in data["cases"]] == [1, 4]
    for case in data["cases"]:
        assert case["outcomes"] == {"SUCCEEDED": 8} and case["failures"] == {}
        assert case["throughput_rps"] > 0 and case["duration_ms"] > 0
        assert set(case["latency_ms"]) == {"p50", "p95", "p99"}
        assert case["latency_ms"]["p50"] is not None
        assert case["queue_wait_ms"]["p99"] is not None
        assert case["status_api_samples"] >= 1 and case["status_api_ms"]["p95"] is not None
        assert case["memory"]["rss_peak_mib"] >= case["memory"]["rss_start_mib"] > 0
        assert "loop_lag_p99_bucket_ms" in case
        assert case["peak_active_runs"] <= case["concurrency"]
        assert case["tokens"] == 0 and case["cost_usd"] == "0"
    assert data["gates"]["passed"] and data["soak"] is None


def test_bench_fault_profiles_show_failure_categories_and_gate(tmp_path: Path) -> None:
    project = new_project(tmp_path)
    (project / "agent.py").write_text(MODEL_AGENT, encoding="utf-8")
    (project / "datasets" / "smoke.jsonl").write_text(
        '{"input": {"text": "hello"}}\n', encoding="utf-8"
    )
    args = ("bench", "--concurrency", "2", "--runs", "4")
    result = runweave(*args, "--fault-profile", "", "--fault-profile", "outage", cwd=project)
    assert result.returncode == 0, result.stdout + result.stderr
    calm, outage = latest(project, "bench")["cases"]
    assert (calm["fault_profile"], outage["fault_profile"]) == ("", "outage")
    assert calm["outcomes"] == {"SUCCEEDED": 4} and calm["tokens"] > 0
    assert calm["model_calls"] == 4
    assert outage["outcomes"] == {"FAILED": 4}
    assert sum(outage["failures"].values()) == 4
    assert all(name.startswith("FAILED:") for name in outage["failures"])

    edit_toml(project, "runs = 32\n", "runs = 32\nmax_failure_rate = 0.0\n")
    gated = runweave(*args, "--fault-profile", "outage", cwd=project)
    assert gated.returncode == 1, gated.stdout + gated.stderr
    verdict = latest(project, "bench")["gates"]
    assert not verdict["passed"] and "failure rate" in verdict["reasons"][0]


def test_bench_soak_records_windows(tmp_path: Path) -> None:
    project = new_project(tmp_path)
    result = runweave("bench", "--concurrency", "2", "--runs", "4", "--soak", "1.5", cwd=project)
    assert result.returncode == 0, result.stdout + result.stderr
    soak = latest(project, "bench")["soak"]
    assert soak["duration_ms"] >= 1500 and soak["concurrency"] == 2
    assert soak["runs"] >= 4 and soak["windows"]
    window = soak["windows"][0]
    assert {"elapsed_ms", "runs", "throughput_rps", "p95_latency_ms", "rss_mib"} <= set(window)


def test_bench_usage_problems_exit_2(tmp_path: Path) -> None:
    project = new_project(tmp_path)
    remote = runweave(
        "bench", "--server", "http://127.0.0.1:9", "--fault-profile", "flaky", cwd=project
    )
    assert remote.returncode == 2 and "fault" in remote.stderr.lower()
    unknown = runweave("bench", "--runs", "2", "--fault-profile", "no-such-profile", cwd=project)
    assert unknown.returncode == 2 and "no-such-profile" in unknown.stderr
    (project / "datasets" / "smoke.jsonl").write_text(
        '{"input": {"text": ""}, "expect": {"status": "REJECTED"}}\n', encoding="utf-8"
    )
    empty = runweave("bench", cwd=project)
    assert empty.returncode == 2 and "input" in empty.stderr.lower()
    assert "Traceback" not in remote.stderr + unknown.stderr + empty.stderr


@pytest.mark.asyncio
async def test_bench_can_load_a_running_server(tmp_path: Path) -> None:
    import httpx

    project = new_project(tmp_path)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    env = {k: v for k, v in os.environ.items() if not k.startswith("RUNWEAVE_")}
    env["PYTHONPATH"] = os.pathsep.join([str(ROOT / "src"), str(project)])
    server = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "runweave.cli",
        "serve",
        "--demo",
        "--api-only",
        "--port",
        str(port),
        "--agent",
        "agent:run",
        cwd=project,
        env=env,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    url = f"http://127.0.0.1:{port}"
    try:
        async with httpx.AsyncClient() as client:
            for _ in range(200):
                try:
                    if (await client.get(f"{url}/health/ready")).status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(0.1)
        result = await asyncio.to_thread(
            runweave, "bench", "--server", url, "--concurrency", "2", "--runs", "4", cwd=project
        )
    finally:
        server.kill()
        await server.wait()
    assert result.returncode == 0, result.stdout + result.stderr
    data = latest(project, "bench")
    assert data["environment"]["mode"] == f"server {url}"
    (case,) = data["cases"]
    assert case["outcomes"] == {"SUCCEEDED": 4} and case["memory"] is None
    assert case["fault_profile"] is None and "server" in data["limitations"]


def test_eval_reaches_the_proxy_in_process(tmp_path: Path) -> None:
    project = new_project(tmp_path)
    (project / "agent.py").write_text(MODEL_AGENT, encoding="utf-8")
    (project / "datasets" / "smoke.jsonl").write_text(
        '{"input": {"text": "hello"}, "expect": {"max_model_calls": 1}}\n', encoding="utf-8"
    )
    result = runweave("eval", cwd=project)
    assert result.returncode == 0, result.stdout + result.stderr
    assert latest(project, "eval")["summary"]["tokens"] > 0


# `runweave shadow`


def test_shadow_blocks_a_candidate_that_regresses(tmp_path: Path) -> None:
    project = new_project(tmp_path)
    source = (project / "agent.py").read_text(encoding="utf-8")
    (project / "agent_v2.py").write_text(
        source.replace('version="0.1.0"', 'version="0.2.0"').replace(
            "longest=longest", "longest=words[0] if words else ''"
        ),
        encoding="utf-8",
    )
    edit_toml(project, "# candidate = { python", "candidate = { python")
    result = runweave("shadow", cwd=project)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "finds-longest" in result.stdout
    data = latest(project, "shadow")
    assert data["kind"] == "shadow" and not data["promotion_eligible"]
    assert data["primary"]["agent"]["version"] == "0.1.0"
    assert data["candidate"]["agent"]["version"] == "0.2.0"
    assert not data["gates"]["no_paired_regressions"]
    assert data["regressions"] == ["finds-longest"]
    pairs = {p["id"]: p for p in data["pairs"]}
    assert set(pairs) == {"counts-words", "finds-longest", "records-steps", "rejects-empty-text"}
    assert pairs["finds-longest"]["differences"]["regressed"]
    assert not pairs["finds-longest"]["differences"]["output_equal"]
    assert pairs["counts-words"]["differences"]["output_equal"]
    assert "output" not in pairs["counts-words"]["primary"]  # outputs are never stored


def test_shadow_promotes_an_equivalent_candidate(tmp_path: Path) -> None:
    project = new_project(tmp_path)
    result = runweave("shadow", "--candidate", "agent:run", cwd=project)
    assert result.returncode == 0, result.stdout + result.stderr
    data = latest(project, "shadow")
    assert data["promotion_eligible"] and all(data["gates"].values())
    assert data["summary"]["pairs"] == 4 and data["summary"]["output_equal"] == 4
    assert "not switched" in data["limitations"]


def test_shadow_needs_a_candidate(tmp_path: Path) -> None:
    project = new_project(tmp_path)
    result = runweave("shadow", cwd=project)
    assert result.returncode == 2 and "candidate" in result.stderr
    remote = runweave(
        "shadow", "--server", "http://127.0.0.1:9", "--candidate", "agent:run", cwd=project
    )
    assert remote.returncode == 2 and "name" in remote.stderr


# Driver cost: a new TCP connection per run cost more CPU than the run itself (benchmarks/README).


def local_ports(client: Any) -> list[int]:
    """Records the client-side port of every response, so tests can see connection reuse."""
    ports: list[int] = []

    async def record(response: Any) -> None:
        ports.append(response.extensions["network_stream"].get_extra_info("client_addr")[1])

    client.event_hooks["response"].append(record)
    return ports


async def test_driver_reuses_one_connection_for_runs_and_their_event_streams(
    tmp_path: Path,
) -> None:
    project = load_project(None, start=new_project(tmp_path))
    async with open_target(project, None) as target:
        ports = local_ports(target.client)
        driver = Driver(target, [{"text": "one two three"}], measure_memory=False)
        first, second = await driver.one(), await driver.one()
    assert (first.status, second.status) == ("SUCCEEDED", "SUCCEEDED")
    assert len(ports) == 4  # a submission and an event stream per run
    assert len(set(ports)) == 1


async def test_server_mode_keeps_a_connection_per_concurrent_request(tmp_path: Path) -> None:
    project = load_project(None, start=new_project(tmp_path))
    async with open_target(project, None) as local:
        url = str(local.client.base_url)
        limits = InProcessLimits(workers=32)
        async with open_target(project, url, limits=limits) as target:
            client = target.client
            ports = local_ports(client)
            for _ in range(2):  # each round holds 32 responses open at once, like 32 streams
                held = [
                    await client.send(client.build_request("GET", "/health/live"), stream=True)
                    for _ in range(32)
                ]
                for response in held:
                    await response.aread()
                    await response.aclose()
    assert len(ports) == 64
    assert len(set(ports)) <= 32  # the second round reuses the first round's connections


async def test_each_client_pool_serves_a_few_runs_so_its_scan_stays_short(tmp_path: Path) -> None:
    project = load_project(None, start=new_project(tmp_path))
    async with open_target(project, None) as target:
        async with client_shards(target, 2 * SHARD_SLOTS + 1) as clients:
            assert len(clients) == 3 and clients[0] is target.client
            assert {str(c.base_url) for c in clients} == {str(target.client.base_url)}
            ports = [local_ports(client) for client in clients]
            driver = Driver(target, [{"text": "one two"}], measure_memory=False)
            driver.clients = clients
            level = await measure_level(driver, 2 * SHARD_SLOTS, 4 * SHARD_SLOTS)
    assert level["outcomes"] == {"SUCCEEDED": 4 * SHARD_SLOTS}
    assert ports[0] and ports[1] and not ports[2]  # 16 slots fill two clients
    # Each client holds a connection per run it serves, plus the probe and /metrics on the first.
    assert all(len(set(used)) <= SHARD_SLOTS + 2 for used in ports)


def test_bench_counts_runs_that_wrote_twice() -> None:
    def snapshot(*resources: str) -> dict[str, Any]:
        steps = [
            {"kind": "effect", "effect": "create_ticket", "operation": "op", "resource": r}
            for r in resources
        ]
        return {"status": "SUCCEEDED", "steps": steps, "usage": {}}

    samples = [
        _sample(snapshot("a", "b"), 0),
        _sample(snapshot("c", "c"), 0),
        _sample(snapshot(), 0),
    ]
    assert [s.duplicate_writes for s in samples] == [1, 0, 0]
    assert _totals(samples)["duplicate_writes"] == 1
