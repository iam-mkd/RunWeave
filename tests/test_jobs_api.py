"""The jobs API: the workspace starts the same `runweave eval|bench|shadow` commands as the CLI.

A job runs the CLI in a child process (so a load test never shares the workspace's event
loop), streams its `--progress` lines as events, and points at the report it saved. Saved
reports are read back from the project's report folder.
"""

import asyncio
import json
import os
import subprocess
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest

from runweave.app import create_app
from runweave.core.settings import Settings
from runweave.scaffold import init_project

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def importable_runweave(monkeypatch: pytest.MonkeyPatch) -> None:
    # Jobs run `python -m runweave.cli`; make the source tree importable in the child.
    path = os.environ.get("PYTHONPATH")
    monkeypatch.setenv(
        "PYTHONPATH", os.pathsep.join([str(ROOT / "src"), *([path] if path else [])])
    )


def project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    init_project(root, "python", force=False)
    return root


@asynccontextmanager
async def workspace(project_file: Path | None) -> AsyncIterator[httpx.AsyncClient]:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        auth_mode="local",
        database_url=None,
        model_provider="fake",
        key_store="none",
        project_file=project_file,
    )
    app = create_app(settings, agents=[])
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://rw") as client,
    ):
        yield client


async def finish(client: httpx.AsyncClient, job_id: str, seconds: float = 120) -> dict[str, Any]:
    events: list[dict[str, Any]] = []
    after = 0
    async with asyncio.timeout(seconds):
        while True:
            body = (await client.get(f"/v1/jobs/{job_id}", params={"after": after})).json()
            events += body["events"]
            if body["events"]:
                after = body["events"][-1]["seq"]
            if body["job"]["status"] not in {"QUEUED", "RUNNING"}:
                return {**body["job"], "events": events}
            await asyncio.sleep(0.1)


def cli(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("RUNWEAVE_")}
    return subprocess.run(
        [sys.executable, "-m", "runweave.cli", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )


# The CLI's machine-readable progress, which jobs stream


def test_progress_prints_json_lines_for_each_command(tmp_path: Path) -> None:
    root = project(tmp_path)
    evaluated = cli("eval", "--progress", cwd=root)
    assert evaluated.returncode == 0, evaluated.stderr
    lines = [json.loads(line) for line in evaluated.stdout.splitlines()]
    assert [e["event"] for e in lines].count("case") == 4
    assert {"id", "passed", "done", "total"} <= set(lines[0])
    assert lines[-1]["event"] == "report" and lines[-1]["id"].startswith("eval-")
    assert lines[-1]["passed"] is True

    benched = cli("bench", "--progress", "--concurrency", "2", "--runs", "6", cwd=root)
    assert benched.returncode == 0, benched.stderr
    events = [json.loads(line) for line in benched.stdout.splitlines()]
    kinds = [e["event"] for e in events]
    assert "level_started" in kinds and "level" in kinds and kinds[-1] == "report"
    level = next(e for e in events if e["event"] == "level")
    assert level["concurrency"] == 2 and level["runs"] == 6
    ticks = [e for e in events if e["event"] == "tick"]
    assert ticks and {"elapsed_ms", "completed", "throughput_rps", "p95_latency_ms"} <= set(
        ticks[-1]
    )

    shadowed = cli("shadow", "--progress", "--candidate", "agent:run", cwd=root)
    assert shadowed.returncode == 0, shadowed.stderr
    events = [json.loads(line) for line in shadowed.stdout.splitlines()]
    assert [e["event"] for e in events].count("pair") == 4
    assert events[-1]["event"] == "report" and events[-1]["id"].startswith("shadow-")


# The jobs API


@pytest.mark.asyncio
async def test_without_a_project_the_api_says_how_to_get_one() -> None:
    async with workspace(None) as client:
        for response in (
            await client.get("/v1/jobs/project"),
            await client.post("/v1/jobs", json={"kind": "eval"}),
            await client.get("/v1/reports"),
        ):
            assert response.status_code == 404
            assert response.json()["detail"]["code"] == "NO_PROJECT"
            assert "runweave init" in response.json()["detail"]["message"]
        assert (await client.get("/v1/jobs")).json() == {"jobs": []}


@pytest.mark.asyncio
async def test_project_info_describes_what_the_screens_can_start(tmp_path: Path) -> None:
    root = project(tmp_path)
    async with workspace(root / "runweave.toml") as client:
        info = (await client.get("/v1/jobs/project")).json()
    assert info["agent"] == {"name": "word-count", "kind": "python"}
    assert info["datasets"] == [{"name": "smoke", "cases": 4}]
    assert info["bench"]["concurrency"] == [1, 4, 16] and info["bench"]["runs"] == 32
    assert {"flaky", "chaos", "outage"} <= set(info["fault_profiles"])
    assert info["shadow"]["candidate"] is None
    assert str(root) not in json.dumps(info)  # no local paths leave the server


@pytest.mark.asyncio
async def test_an_eval_job_streams_cases_and_saves_a_report(tmp_path: Path) -> None:
    root = project(tmp_path)
    async with workspace(root / "runweave.toml") as client:
        started = await client.post("/v1/jobs", json={"kind": "eval", "dataset": "smoke"})
        assert started.status_code == 202, started.text
        job = started.json()
        assert job["kind"] == "eval" and job["status"] == "RUNNING"
        done = await finish(client, job["id"])
        assert done["status"] == "COMPLETED" and done["passed"] is True, done
        assert [e["event"] for e in done["events"]].count("case") == 4
        assert done["report_id"].startswith("eval-")
        listed = (await client.get("/v1/reports", params={"kind": "eval"})).json()["reports"]
        assert [r["id"] for r in listed] == [done["report_id"]]
        assert listed[0]["headline"]["passed"] == 4 and listed[0]["passed"] is True
        report = (await client.get(f"/v1/reports/{done['report_id']}")).json()
        assert report["kind"] == "eval" and len(report["cases"]) == 4
        assert (await client.get("/v1/jobs")).json()["jobs"][0]["id"] == job["id"]


@pytest.mark.asyncio
async def test_a_bench_job_streams_live_ticks(tmp_path: Path) -> None:
    root = project(tmp_path)
    async with workspace(root / "runweave.toml") as client:
        started = await client.post(
            "/v1/jobs",
            json={"kind": "bench", "concurrency": [2], "runs": 10, "fault_profiles": [""]},
        )
        assert started.status_code == 202, started.text
        done = await finish(client, started.json()["id"])
        assert done["status"] == "COMPLETED", done
        kinds = [e["event"] for e in done["events"]]
        assert "tick" in kinds and "level" in kinds
        seqs = [e["seq"] for e in done["events"]]
        assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
        (summary,) = (await client.get("/v1/reports", params={"kind": "bench"})).json()["reports"]
        assert summary["headline"]["levels"] == 1 and summary["headline"]["throughput_rps"] > 0


@pytest.mark.asyncio
async def test_one_job_at_a_time_and_jobs_can_be_cancelled(tmp_path: Path) -> None:
    root = project(tmp_path)
    async with workspace(root / "runweave.toml") as client:
        long = await client.post(
            "/v1/jobs",
            json={"kind": "bench", "concurrency": [1], "runs": 1, "soak_seconds": 60},
        )
        assert long.status_code == 202
        busy = await client.post("/v1/jobs", json={"kind": "eval"})
        assert busy.status_code == 409 and busy.json()["detail"]["code"] == "JOB_RUNNING"
        cancelled = await client.post(f"/v1/jobs/{long.json()['id']}/cancel")
        assert cancelled.status_code == 200
        done = await finish(client, long.json()["id"], seconds=20)
        assert done["status"] == "CANCELLED" and done["report_id"] is None
        again = await client.post("/v1/jobs", json={"kind": "eval"})
        assert again.status_code == 202


@pytest.mark.asyncio
async def test_a_failing_command_fails_the_job_with_its_message(tmp_path: Path) -> None:
    root = project(tmp_path)
    async with workspace(root / "runweave.toml") as client:
        started = await client.post("/v1/jobs", json={"kind": "eval", "dataset": "missing"})
        done = await finish(client, started.json()["id"])
    assert done["status"] == "FAILED" and "missing" in done["error"]
    assert "Traceback" not in done["error"]


@pytest.mark.asyncio
async def test_a_failed_gate_completes_the_job_as_not_passed(tmp_path: Path) -> None:
    root = project(tmp_path)
    with (root / "datasets" / "smoke.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(
            '{"id": "wrong", "input": {"text": "a b"}, "expect": {"output": {"words": 3}}}\n'
        )
    async with workspace(root / "runweave.toml") as client:
        started = await client.post("/v1/jobs", json={"kind": "eval"})
        done = await finish(client, started.json()["id"])
    assert done["status"] == "COMPLETED" and done["passed"] is False and done["report_id"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {"kind": "deploy"},
        {"kind": "eval", "dataset": "../etc"},
        {"kind": "bench", "concurrency": [0]},
        {"kind": "bench", "concurrency": list(range(1, 20))},
        {"kind": "bench", "runs": 0},
        {"kind": "bench", "fault_profiles": ["a b"]},
        {"kind": "bench", "soak_seconds": 99999},
        {"kind": "shadow", "candidate": "x; rm -rf /"},
        {"kind": "eval", "surprise": True},
    ],
)
async def test_job_requests_are_validated(tmp_path: Path, body: dict[str, Any]) -> None:
    root = project(tmp_path)
    async with workspace(root / "runweave.toml") as client:
        response = await client.post("/v1/jobs", json=body)
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_reports_and_jobs_are_looked_up_by_strict_ids(tmp_path: Path) -> None:
    root = project(tmp_path)
    secret = root / ".runweave" / "secret.json"
    secret.parent.mkdir(parents=True, exist_ok=True)
    secret.write_text('{"kind": "eval"}', encoding="utf-8")
    async with workspace(root / "runweave.toml") as client:
        for path in (
            "/v1/reports/eval-20261119T000000Z-abcdef",
            "/v1/reports/..%2Fsecret",
            "/v1/reports/secret",
            "/v1/jobs/job-000000000000",
            "/v1/jobs/../reports",
        ):
            response = await client.get(path)
            assert response.status_code in {404, 422}, path
        assert (await client.get("/v1/reports", params={"kind": "nope"})).status_code == 422
        assert (await client.post("/v1/jobs/job-000000000000/cancel")).status_code == 404
