import asyncio
from uuid import UUID

import pytest

from runweave.agents.contracts import IncidentRequest, IncidentResult
from runweave.orchestration.models import RunStatus
from runweave.orchestration.registry import RunRegistry
from runweave.orchestration.scheduler import AdmissionRejected, Scheduler


class ControlledExecutor:
    def __init__(self, target: int = 1) -> None:
        self.release = asyncio.Event()
        self.reached = asyncio.Event()
        self.starts: asyncio.Queue[str] = asyncio.Queue()
        self.target = target
        self.active = 0
        self.peak = 0

    async def run(self, incident: IncidentRequest) -> IncidentResult:
        self.active += 1
        self.peak = max(self.peak, self.active)
        self.starts.put_nowait(incident.service)
        if self.active >= self.target:
            self.reached.set()
        try:
            await self.release.wait()
            if incident.service == "broken":
                raise ValueError("Sensitive downstream error must not be exposed")
            return IncidentResult(summary="Mock result", severity="low", evidence_ids=("mock:1",))
        finally:
            self.active -= 1


def scheduler_for(
    executor: ControlledExecutor, workers: int = 1, capacity: int = 10, *, shutdown_grace: float = 0
) -> Scheduler:
    return Scheduler(
        executor,
        RunRegistry(1000, 60),
        worker_count=workers,
        queue_capacity=capacity,
        cleanup_interval=60,
        shutdown_grace=shutdown_grace,
    )


def request(service: str = "payments") -> IncidentRequest:
    return IncidentRequest(service=service, description="Latency")


def state(scheduler: Scheduler, run_id: UUID) -> RunStatus:
    snapshot = scheduler.get(run_id)
    assert snapshot is not None
    return snapshot.status


@pytest.mark.asyncio
async def test_128_runs_overlap_and_execution_never_exceeds_worker_limit() -> None:
    executor = ControlledExecutor(target=128)
    scheduler = scheduler_for(executor, workers=128, capacity=200)
    async with scheduler.running():
        runs = [scheduler.submit(request()) for _ in range(180)]
        await asyncio.wait_for(executor.reached.wait(), 3)
        assert executor.active == executor.peak == 128
        assert sum(state(scheduler, run.id) == RunStatus.RUNNING for run in runs) == 128
        executor.release.set()
        await asyncio.wait_for(scheduler.join(), 3)
        assert all(state(scheduler, run.id) == RunStatus.SUCCEEDED for run in runs)
    assert executor.active == 0


@pytest.mark.asyncio
async def test_queue_overflow_rejects_admission_without_harming_readiness() -> None:
    executor = ControlledExecutor()
    scheduler = scheduler_for(executor, capacity=1)
    with pytest.raises(AdmissionRejected, match="SCHEDULER_UNAVAILABLE"):
        scheduler.submit(request())
    async with scheduler.running():
        scheduler.submit(request())
        await asyncio.wait_for(executor.reached.wait(), 1)
        queued = scheduler.submit(request())
        with pytest.raises(AdmissionRejected, match="QUEUE_FULL"):
            scheduler.submit(request())
        assert scheduler.ready
        assert scheduler.get(queued.id) is not None
        executor.release.set()
        await asyncio.wait_for(scheduler.join(), 1)


@pytest.mark.asyncio
async def test_cancelled_queued_run_is_skipped_and_cancellation_is_idempotent() -> None:
    executor = ControlledExecutor()
    scheduler = scheduler_for(executor)
    async with scheduler.running():
        scheduler.submit(request())
        await asyncio.wait_for(executor.reached.wait(), 1)
        queued = scheduler.submit(request("disputes"))
        cancelled = scheduler.cancel(queued.id)
        assert cancelled is not None and cancelled.status == RunStatus.CANCELLED
        assert scheduler.cancel(queued.id) == cancelled
        executor.release.set()
        await asyncio.wait_for(scheduler.join(), 1)
        assert executor.starts.qsize() == 1


@pytest.mark.asyncio
async def test_running_cancellation_releases_worker_for_the_next_run() -> None:
    executor = ControlledExecutor()
    scheduler = scheduler_for(executor)
    async with scheduler.running():
        first = scheduler.submit(request())
        assert await asyncio.wait_for(executor.starts.get(), 1) == "payments"
        next_run = scheduler.submit(request("disputes"))
        scheduler.cancel(first.id)
        assert await asyncio.wait_for(executor.starts.get(), 1) == "disputes"
        assert executor.active == 1
        executor.release.set()
        await asyncio.wait_for(scheduler.join(), 1)
        assert state(scheduler, first.id) == RunStatus.CANCELLED
        assert state(scheduler, next_run.id) == RunStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_failed_run_is_sanitized_and_does_not_kill_worker() -> None:
    executor = ControlledExecutor()
    executor.release.set()
    scheduler = scheduler_for(executor)
    async with scheduler.running():
        failed = scheduler.submit(request("broken"))
        good = scheduler.submit(request())
        await asyncio.wait_for(scheduler.join(), 1)
        snapshot = scheduler.get(failed.id)
        assert snapshot is not None and snapshot.status == RunStatus.FAILED
        assert snapshot.error is not None and snapshot.error.code == "RUN_FAILED"
        assert "Sensitive" not in snapshot.model_dump_json()
        assert state(scheduler, good.id) == RunStatus.SUCCEEDED
        assert scheduler.cancel(good.id) == scheduler.get(good.id)
        assert scheduler.ready


@pytest.mark.asyncio
async def test_shutdown_cancels_pending_work_and_awaits_active_cleanup() -> None:
    executor = ControlledExecutor()
    scheduler = scheduler_for(executor, shutdown_grace=0.03)
    async with asyncio.timeout(1), scheduler.running():
        active = scheduler.submit(request())
        await asyncio.wait_for(executor.reached.wait(), 1)
        queued = scheduler.submit(request())
    assert executor.active == 0
    assert not scheduler.ready
    assert state(scheduler, active.id) == RunStatus.CANCELLED
    assert state(scheduler, queued.id) == RunStatus.CANCELLED
    await asyncio.wait_for(scheduler.join(), 1)
    assert not any(task.get_name().startswith("runweave-") for task in asyncio.all_tasks())


@pytest.mark.asyncio
async def test_shutdown_drains_admitted_runs_and_rejects_new_work() -> None:
    executor = ControlledExecutor()
    scheduler = scheduler_for(executor, shutdown_grace=1)

    async def release_during_drain() -> None:
        assert not scheduler.ready
        with pytest.raises(AdmissionRejected, match="SCHEDULER_UNAVAILABLE"):
            scheduler.submit(request())
        executor.release.set()

    async with scheduler.running():
        active = scheduler.submit(request())
        await asyncio.wait_for(executor.reached.wait(), 1)
        queued = scheduler.submit(request())
        release = asyncio.create_task(release_during_drain())
    await release
    assert state(scheduler, active.id) == RunStatus.SUCCEEDED
    assert state(scheduler, queued.id) == RunStatus.SUCCEEDED


class SlowCleanupExecutor:
    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.cleaning = asyncio.Event()
        self.release = asyncio.Event()
        self.started: list[str] = []

    async def run(self, incident: IncidentRequest) -> IncidentResult:
        self.started.append(incident.service)
        self.entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            self.cleaning.set()
            await self.release.wait()
        raise AssertionError("Unreachable")


@pytest.mark.asyncio
async def test_deadline_includes_queue_wait_and_skips_expired_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executor = SlowCleanupExecutor()
    scheduler = Scheduler(
        executor,
        RunRegistry(10, 60),
        worker_count=1,
        queue_capacity=2,
        cleanup_interval=60,
        run_timeout=0.05,
        shutdown_grace=0,
    )
    queued_expired = asyncio.Event()
    expire = scheduler._expire_queued

    def observe_expiry(run_id: UUID) -> None:
        expire(run_id)
        queued_expired.set()

    monkeypatch.setattr(scheduler, "_expire_queued", observe_expiry)
    async with scheduler.running():
        active = scheduler.submit(request())
        queued = scheduler.submit(request("disputes"))
        try:
            await asyncio.wait_for(executor.cleaning.wait(), 2)
            await asyncio.wait_for(queued_expired.wait(), 2)
            expired = scheduler.get(queued.id)
            assert expired is not None and expired.status == RunStatus.TIMED_OUT
            assert expired.started_at is None
            assert expired.error is not None and expired.error.code == "RUN_DEADLINE_EXCEEDED"
        finally:
            executor.release.set()
        await asyncio.wait_for(scheduler.join(), 1)
        assert state(scheduler, active.id) == RunStatus.TIMED_OUT
        assert executor.started == ["payments"]


@pytest.mark.asyncio
async def test_shutdown_does_not_interrupt_cleanup_after_user_cancellation() -> None:
    executor = SlowCleanupExecutor()
    scheduler = Scheduler(
        executor,
        RunRegistry(10, 60),
        worker_count=1,
        queue_capacity=1,
        cleanup_interval=60,
        shutdown_grace=0,
    )

    async def run_until_shutdown() -> None:
        async with scheduler.running():
            run = scheduler.submit(request())
            await asyncio.wait_for(executor.entered.wait(), 1)
            scheduler.cancel(run.id)
            await asyncio.wait_for(executor.cleaning.wait(), 1)

    shutdown = asyncio.create_task(run_until_shutdown())
    try:
        await asyncio.wait_for(executor.cleaning.wait(), 1)
        await asyncio.sleep(0.03)
        assert not scheduler.ready
        assert not shutdown.done()
    finally:
        executor.release.set()
        await asyncio.wait_for(shutdown, 1)


@pytest.mark.asyncio
async def test_scheduler_failure_fails_readiness_and_is_propagated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executor = ControlledExecutor()
    registry = RunRegistry(10, 60)
    scheduler = Scheduler(executor, registry, worker_count=1, queue_capacity=1, cleanup_interval=60)

    def broken_claim(run_id: object) -> IncidentRequest:
        raise RuntimeError("Registry unavailable")

    monkeypatch.setattr(registry, "claim", broken_claim)
    with pytest.raises(ExceptionGroup) as caught:
        async with asyncio.timeout(1), scheduler.running():
            scheduler.submit(request())
            await asyncio.Event().wait()
    assert caught.value.subgroup(RuntimeError) is not None
    assert not scheduler.ready


class LateResultExecutor:
    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def run(self, incident: IncidentRequest) -> IncidentResult:
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return IncidentResult(summary="Late result", severity="low", evidence_ids=("mock:1",))
        raise AssertionError("Unreachable")


@pytest.mark.asyncio
async def test_late_success_cannot_overwrite_cancellation() -> None:
    executor = LateResultExecutor()
    scheduler = Scheduler(
        executor, RunRegistry(10, 60), worker_count=1, queue_capacity=1, cleanup_interval=60
    )
    async with scheduler.running():
        run = scheduler.submit(request())
        await asyncio.wait_for(executor.started.wait(), 1)
        scheduler.cancel(run.id)
        await asyncio.wait_for(scheduler.join(), 1)
        snapshot = scheduler.get(run.id)
        assert snapshot is not None and snapshot.status == RunStatus.CANCELLED
        assert snapshot.result is None


@pytest.mark.asyncio
async def test_cleanup_prunes_history_without_polling(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [0.0]
    registry = RunRegistry(10, 1, clock=lambda: now[0])
    executor = ControlledExecutor()
    executor.release.set()
    scheduler = Scheduler(
        executor, registry, worker_count=1, queue_capacity=1, cleanup_interval=0.01
    )
    pruned = asyncio.Event()
    original_prune = registry.prune

    def observe_prune() -> None:
        original_prune()
        if now[0] >= 1:
            pruned.set()

    monkeypatch.setattr(registry, "prune", observe_prune)
    async with scheduler.running():
        run = scheduler.submit(request())
        await asyncio.wait_for(scheduler.join(), 1)
        now[0] = 2
        await asyncio.wait_for(pruned.wait(), 1)
        assert registry.get(run.id) is None
