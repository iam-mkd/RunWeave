"""Testing jobs the workspace starts: the same `runweave eval|bench|shadow` the CLI runs.

Each job runs the CLI in a child process with `--progress`, in the project folder, so a load
test never shares the workspace's event loop and the UI and CLI produce identical reports.
Progress lines become numbered events the screens poll. One job runs at a time; the last
few are kept in memory. Cancelling closes the child's stdin, which makes the command stop
cleanly (and stop any agent process it started) before it is killed as a last resort.

Saved reports are read back from the project's report folder by strict id only.
"""

import asyncio
import json
import os
import re
import sys
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from runweave.evals.cases import CaseError, load_cases
from runweave.project import NAME_PATTERN, Project, load_project
from runweave.proxy.faults import BUILT_IN_PROFILES

JOB_ID = re.compile(r"^job-[0-9a-f]{12}$")
REPORT_ID = re.compile(r"^(eval|bench|shadow)-\d{8}T\d{6}Z-[0-9a-f]{6}$")
MAX_EVENTS = 5000
MAX_EVENTS_PER_READ = 500
MAX_LINE_BYTES = 1 << 20
MAX_REPORT_BYTES = 32 << 20
HISTORY = 20
STOP_GRACE_SECONDS = 10

FaultProfileName = Annotated[str, StringConstraints(pattern=r"^(?:[a-z0-9][a-z0-9_-]{0,39})?$")]
Level = Annotated[int, Field(ge=1, le=256)]
Candidate = Annotated[
    str,
    StringConstraints(
        max_length=300,
        pattern=r"^(?:[A-Za-z_][\w.]*:[A-Za-z_]\w*|https?://[A-Za-z0-9.\-]+(?::\d{1,5})?(?:/[\w./-]*)?)$",
    ),
]
Kind = Literal["eval", "bench", "shadow"]
Status = Literal["RUNNING", "COMPLETED", "FAILED", "CANCELLED"]


class NoProject(LookupError):
    """The workspace was started without a runweave.toml."""


class JobRunning(RuntimeError):
    """Another job is still running."""


class JobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Kind
    dataset: str | None = Field(default=None, pattern=NAME_PATTERN)
    concurrency: list[Level] | None = Field(default=None, min_length=1, max_length=12)
    runs: int | None = Field(default=None, ge=1, le=10000)
    fault_profiles: list[FaultProfileName] | None = Field(default=None, min_length=1, max_length=12)
    soak_seconds: float | None = Field(default=None, ge=0, le=3600)
    candidate: Candidate | None = None

    @model_validator(mode="after")
    def options_fit_the_kind(self) -> "JobRequest":
        bench = (self.concurrency, self.runs, self.fault_profiles, self.soak_seconds)
        if self.kind != "bench" and any(value is not None for value in bench):
            raise ValueError("concurrency, runs, fault_profiles and soak_seconds are bench options")
        if self.kind != "shadow" and self.candidate is not None:
            raise ValueError("candidate is a shadow option")
        return self

    def argv(self) -> list[str]:
        """Arguments for `runweave`; every value is already validated, and no shell is used."""
        args = [self.kind, "--progress", "--watch-stdin"]
        if self.dataset:
            args += ["--dataset", self.dataset]
        if self.concurrency:
            args += ["--concurrency", *(str(level) for level in self.concurrency)]
        if self.runs is not None:
            args += ["--runs", str(self.runs)]
        for profile in self.fault_profiles or ():
            args.append(f"--fault-profile={profile}")
        if self.soak_seconds is not None:
            args += ["--soak", str(self.soak_seconds)]
        if self.candidate:
            args.append(f"--candidate={self.candidate}")
        return args


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class Job:
    id: str
    request: JobRequest
    status: Status = "RUNNING"
    created_at: str = field(default_factory=_now)
    finished_at: str | None = None
    report_id: str | None = None
    passed: bool | None = None
    error: str | None = None
    events: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=MAX_EVENTS))
    seq: int = 0
    cancel_requested: bool = False
    process: asyncio.subprocess.Process | None = None
    task: asyncio.Task[None] | None = None

    def add(self, event: dict[str, Any]) -> None:
        self.seq += 1
        self.events.append({**event, "seq": self.seq})

    def after(self, seq: int) -> list[dict[str, Any]]:
        return [e for e in self.events if e["seq"] > seq][:MAX_EVENTS_PER_READ]

    def view(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.request.kind,
            "options": self.request.model_dump(exclude_none=True, exclude={"kind"}),
            "status": self.status,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
            "report_id": self.report_id,
            "passed": self.passed,
            "error": self.error,
            "last_seq": self.seq,
        }


def _error_text(stderr: str, code: int | None) -> str:
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    if lines:
        last = lines[-1]
        return last.removeprefix("runweave: ")[:500]
    return f"The command stopped unexpectedly (exit code {code})"


class JobManager:
    def __init__(self, project_file: Path | None, *, python: str = sys.executable) -> None:
        self.project_file = project_file
        self.python = python
        self.jobs: dict[str, Job] = {}
        self.closed = False

    def project(self) -> Project:
        if self.project_file is None:
            raise NoProject()
        return load_project(self.project_file)

    def active(self) -> Job | None:
        return next((job for job in self.jobs.values() if job.status == "RUNNING"), None)

    def start(self, request: JobRequest) -> Job:
        project = self.project()
        if self.closed:
            raise JobRunning()
        if self.active() is not None:
            raise JobRunning()
        job = Job(id=f"job-{uuid4().hex[:12]}", request=request)
        self.jobs[job.id] = job
        while len(self.jobs) > HISTORY:
            oldest = next(iter(self.jobs))
            if self.jobs[oldest].status == "RUNNING":
                break
            del self.jobs[oldest]
        job.task = asyncio.create_task(self._run(job, project.root), name=f"runweave-{job.id}")
        return job

    def get(self, job_id: str) -> Job | None:
        return self.jobs.get(job_id) if JOB_ID.match(job_id) else None

    def recent(self) -> list[Job]:
        return sorted(self.jobs.values(), key=lambda job: job.created_at, reverse=True)

    async def cancel(self, job: Job) -> None:
        if job.status != "RUNNING":
            return
        job.cancel_requested = True
        process = job.process
        if process is not None and process.stdin is not None and not process.stdin.is_closing():
            process.stdin.close()

    async def close(self) -> None:
        self.closed = True
        running = [job for job in self.jobs.values() if job.task and not job.task.done()]
        for job in running:
            await self.cancel(job)
        for job in running:
            assert job.task is not None
            try:
                async with asyncio.timeout(STOP_GRACE_SECONDS + 5):
                    await asyncio.shield(job.task)
            except TimeoutError:
                job.task.cancel()
                await asyncio.gather(job.task, return_exceptions=True)

    async def _run(self, job: Job, root: Path) -> None:
        env = {k: v for k, v in os.environ.items() if not k.startswith("RUNWEAVE_")}
        env["PYTHONUNBUFFERED"] = "1"
        stderr_tail: deque[str] = deque(maxlen=20)
        try:
            process = await asyncio.create_subprocess_exec(
                self.python,
                "-m",
                "runweave.cli",
                *job.request.argv(),
                cwd=root,
                env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=MAX_LINE_BYTES,
            )
        except OSError as error:
            self._finish(job, "FAILED", error=f"Cannot start the command: {error.strerror}")
            return
        job.process = process
        if job.cancel_requested and process.stdin is not None:
            process.stdin.close()  # cancelled while the command was starting
        try:
            await asyncio.gather(
                self._read_events(job, process), self._read_errors(process, stderr_tail)
            )
            if job.cancel_requested:
                try:
                    async with asyncio.timeout(STOP_GRACE_SECONDS):
                        await process.wait()
                except TimeoutError:
                    process.kill()
            code = await process.wait()
        except asyncio.CancelledError:
            if process.returncode is None:
                process.kill()
                await process.wait()
            self._finish(job, "CANCELLED")
            raise
        if job.cancel_requested:
            self._finish(job, "CANCELLED")
        elif code in (0, 1) and job.report_id is not None:
            self._finish(job, "COMPLETED")
        else:
            self._finish(job, "FAILED", error=_error_text("\n".join(stderr_tail), code))

    async def _read_events(self, job: Job, process: asyncio.subprocess.Process) -> None:
        assert process.stdout is not None
        while True:
            try:
                line = await process.stdout.readline()
            except ValueError:  # a line over the limit: skip what is buffered
                continue
            if not line:
                return
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict) or not isinstance(event.get("event"), str):
                continue
            if event["event"] == "report":
                report_id = event.get("id")
                if isinstance(report_id, str) and REPORT_ID.match(report_id):
                    job.report_id = report_id
                    job.passed = event.get("passed") is True
            job.add(event)

    @staticmethod
    async def _read_errors(process: asyncio.subprocess.Process, tail: deque[str]) -> None:
        assert process.stderr is not None
        while True:
            try:
                line = await process.stderr.readline()
            except ValueError:
                continue
            if not line:
                return
            tail.append(line.decode(errors="replace")[:1000])

    def _finish(self, job: Job, status: Status, *, error: str | None = None) -> None:
        job.status = status
        job.error = error
        job.finished_at = _now()
        if status != "COMPLETED":
            job.passed = None if status == "CANCELLED" else job.passed
        job.process = None


# Project and reports, as the screens see them


def _candidate_text(project: Project) -> str | None:
    candidate = project.shadow.candidate
    if candidate is None:
        return None
    if candidate.python:
        return candidate.python
    if candidate.http:
        return candidate.http
    return " ".join(candidate.command or ())


def _case_count(path: Path) -> int | None:
    try:
        return len(load_cases(path))
    except CaseError:
        return None


def describe(project: Project) -> dict[str, Any]:
    """What the screens can start. Blocking (reads datasets): call in a thread."""
    shadow = project.shadow
    return {
        "name": project.root.name,
        "agent": {"name": project.agent.name, "kind": project.agent.kind},
        "datasets": [{"name": d.name, "cases": _case_count(d.path)} for d in project.datasets],
        "eval": {
            "min_pass_rate": project.eval.min_pass_rate,
            "baseline": project.eval.baseline is not None and project.eval.baseline.is_file(),
        },
        "bench": {
            "concurrency": list(project.bench.concurrency),
            "runs": project.bench.runs,
            "fault_profiles": list(project.bench.fault_profiles or ("",)),
            "soak_seconds": project.bench.soak_seconds,
        },
        "shadow": {
            "candidate": _candidate_text(project),
            "max_cost_ratio": shadow.max_cost_ratio,
            "max_latency_ratio": shadow.max_latency_ratio,
            "latency_slack_ms": shadow.latency_slack_ms,
            "require_output_parity": shadow.require_output_parity,
        },
        "fault_profiles": sorted(BUILT_IN_PROFILES),
    }


def _headline(report: dict[str, Any]) -> tuple[bool | None, dict[str, Any]]:
    kind = report.get("kind")
    if kind == "eval":
        s = report.get("summary", {})
        return report.get("gates", {}).get("passed"), {
            "passed": s.get("passed"),
            "total": s.get("total"),
            "pass_rate": s.get("pass_rate"),
            "p95_latency_ms": s.get("p95_latency_ms"),
        }
    if kind == "bench":
        cases = report.get("cases", [])
        p95s = [c["latency_ms"]["p95"] for c in cases if c.get("latency_ms", {}).get("p95")]
        return report.get("gates", {}).get("passed"), {
            "levels": len(cases),
            "throughput_rps": max((c.get("throughput_rps", 0) for c in cases), default=0),
            "p95_latency_ms": max(p95s, default=None),
            "failures": sum(sum(c.get("failures", {}).values()) for c in cases),
        }
    return report.get("promotion_eligible"), {
        "pairs": report.get("summary", {}).get("pairs"),
        "regressions": len(report.get("regressions", [])),
        "candidate": report.get("candidate", {}).get("agent"),
    }


def _read(path: Path) -> dict[str, Any] | None:
    try:
        if path.stat().st_size > MAX_REPORT_BYTES:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def read_report(folder: Path, report_id: str) -> dict[str, Any] | None:
    """Blocking: call in a thread."""
    if not REPORT_ID.match(report_id):
        return None
    report = _read(folder / report_id / "report.json")
    return report if report and report.get("id") == report_id else None


def list_reports(folder: Path, kind: Kind | None, limit: int = 50) -> list[dict[str, Any]]:
    """Newest first. Blocking: call in a thread."""
    if not folder.is_dir():
        return []
    names = [
        p.name
        for p in folder.iterdir()
        if REPORT_ID.match(p.name) and (kind is None or p.name.startswith(f"{kind}-"))
    ]
    names.sort(key=lambda name: name.split("-", 1)[1], reverse=True)
    summaries = []
    for name in names[:limit]:
        report = read_report(folder, name)
        if report is None:
            continue
        passed, headline = _headline(report)
        agent = report.get("agent") or report.get("primary", {}).get("agent") or {}
        summaries.append(
            {
                "id": name,
                "kind": report.get("kind"),
                "created_at": report.get("created_at"),
                "agent": {"name": agent.get("name"), "version": agent.get("version")},
                "dataset": report.get("dataset", {}).get("name"),
                "passed": passed,
                "headline": headline,
            }
        )
    return summaries
