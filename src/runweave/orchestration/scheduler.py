import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from uuid import UUID

import httpx

from runweave.agents.contracts import AgentExecutor, IncidentRequest
from runweave.deadline import RUN_DEADLINE, DeadlineExceeded, deadline_scope
from runweave.orchestration.models import RunError, RunSnapshot, RunStatus
from runweave.orchestration.registry import RunRegistry


class AdmissionRejected(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class Scheduler:
    def __init__(
        self,
        executor: AgentExecutor,
        registry: RunRegistry,
        *,
        worker_count: int,
        queue_capacity: int,
        cleanup_interval: float,
        run_timeout: float = 30.0,
        shutdown_grace: float = 5.0,
    ) -> None:
        if worker_count < 1 or queue_capacity < 1 or cleanup_interval <= 0:
            raise ValueError("Scheduler limits must be positive")
        if run_timeout <= 0 or shutdown_grace < 0:
            raise ValueError("Run timeout must be positive and shutdown grace nonnegative")
        self._executor = executor
        self._registry = registry
        self._worker_count = worker_count
        self._cleanup_interval = cleanup_interval
        self._run_timeout = run_timeout
        self._shutdown_grace = shutdown_grace
        self._queue: asyncio.Queue[UUID] = asyncio.Queue(maxsize=queue_capacity)
        self._queued_deadlines: dict[UUID, asyncio.TimerHandle] = {}
        self._active: dict[UUID, asyncio.Task[None]] = {}
        self._accepting = False
        self._closing = False
        self._started = False
        self._telemetry = registry.telemetry

    @property
    def ready(self) -> bool:
        return self._accepting

    def submit(self, incident: IncidentRequest) -> RunSnapshot:
        if not self.ready:
            self._telemetry.rejected["unavailable"] += 1
            raise AdmissionRejected("SCHEDULER_UNAVAILABLE")
        if self._queue.full():
            self._telemetry.rejected["queue_full"] += 1
            raise AdmissionRejected("QUEUE_FULL")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._run_timeout
        # No await between capacity check, record creation, and enqueue.
        snapshot = self._registry.create(incident)
        self._queue.put_nowait(snapshot.id)
        self._queued_deadlines[snapshot.id] = loop.call_at(
            deadline, self._expire_queued, snapshot.id
        )
        self._telemetry.admitted += 1
        return snapshot

    def metrics(self) -> str:
        return self._telemetry.render(self._queue.qsize(), len(self._active), len(self._registry))

    def get(self, run_id: UUID) -> RunSnapshot | None:
        return self._registry.get(run_id)

    def cancel(self, run_id: UUID) -> RunSnapshot | None:
        snapshot = self.get(run_id)
        if snapshot is None or snapshot.status.terminal:
            return snapshot
        cancelled = self._registry.finish(run_id, RunStatus.CANCELLED)
        timer = self._queued_deadlines.pop(run_id, None)
        if timer is not None:
            timer.cancel()
        task = self._active.get(run_id)
        if task is not None:
            task.cancel()
        return cancelled

    async def join(self) -> None:
        await self._queue.join()

    @asynccontextmanager
    async def running(self) -> AsyncIterator[None]:
        if self._started:
            raise RuntimeError("A scheduler instance can only be started once")
        self._started = True
        async with asyncio.TaskGroup() as group:
            workers = [
                group.create_task(self._worker(), name=f"runweave-worker-{index}")
                for index in range(self._worker_count)
            ]
            cleaner = group.create_task(self._cleanup(), name="runweave-history-cleanup")
            lag_sampler = group.create_task(self._telemetry.sample_loop_lag(), name="runweave-lag")
            self._accepting = True
            try:
                yield
                can_drain = self._accepting
                self._accepting = False
                if can_drain and self._shutdown_grace > 0:
                    with suppress(TimeoutError):
                        async with asyncio.timeout(self._shutdown_grace):
                            await self.join()
            finally:
                self._accepting = False
                self._closing = True
                self._registry.cancel_unfinished()
                for timer in self._queued_deadlines.values():
                    timer.cancel()
                self._queued_deadlines.clear()
                for task in [*workers, cleaner, lag_sampler]:
                    if not task.cancelling():
                        task.cancel()
                while not self._queue.empty():
                    self._queue.get_nowait()
                    self._queue.task_done()

    def _expire_queued(self, run_id: UUID) -> None:
        self._queued_deadlines.pop(run_id, None)
        try:
            self._finish_timeout(run_id, RUN_DEADLINE)
        except Exception:
            self._accepting = False
            raise

    def _finish_timeout(self, run_id: UUID, code: str) -> None:
        self._registry.finish(
            run_id,
            RunStatus.TIMED_OUT,
            error=RunError(code=code, message="Execution deadline exceeded"),
        )

    async def _worker(self) -> None:
        try:
            while not self._closing:
                run_id = await self._queue.get()
                try:
                    timer = self._queued_deadlines.pop(run_id, None)
                    if timer is None:
                        continue
                    timer.cancel()
                    deadline = timer.when()
                    if asyncio.get_running_loop().time() >= deadline:
                        self._finish_timeout(run_id, RUN_DEADLINE)
                        continue
                    incident = self._registry.claim(run_id)
                    if incident is None:
                        continue
                    task = asyncio.create_task(
                        self._execute(run_id, incident, deadline), name=f"runweave-run-{run_id}"
                    )
                    self._active[run_id] = task
                    self._telemetry.peak_runs = max(self._telemetry.peak_runs, len(self._active))
                    try:
                        await asyncio.shield(task)
                    except asyncio.CancelledError:
                        worker = asyncio.current_task()
                        if worker is not None and worker.cancelling():
                            if not task.done() and not task.cancelling():
                                task.cancel()
                            with suppress(asyncio.CancelledError):
                                await task
                            raise
                    finally:
                        self._active.pop(run_id, None)
                finally:
                    self._queue.task_done()
        finally:
            if not self._closing:
                self._accepting = False

    async def _execute(self, run_id: UUID, incident: IncidentRequest, deadline: float) -> None:
        try:
            with self._telemetry.span("agent.run", run_id=run_id):
                async with deadline_scope(deadline, RUN_DEADLINE):
                    result = await self._executor.run(incident)
        except DeadlineExceeded as exc:
            self._finish_timeout(run_id, exc.code)
        except asyncio.CancelledError:
            self._registry.finish(run_id, RunStatus.CANCELLED)
            raise
        except Exception as exc:
            if isinstance(exc, httpx.HTTPStatusError):
                status_group = exc.response.status_code // 100
                category = {4: "http_4xx", 5: "http_5xx"}.get(status_group, "http_other")
            elif isinstance(exc, httpx.TransportError):
                category = "transport"
            elif isinstance(exc, ValueError):
                category = "validation"
            else:
                category = "execution"
            self._telemetry.failure_kinds[category] += 1
            self._registry.finish(
                run_id,
                RunStatus.FAILED,
                error=RunError(code="RUN_FAILED", message="Agent execution failed"),
            )
        else:
            self._registry.finish(run_id, RunStatus.SUCCEEDED, result=result)

    async def _cleanup(self) -> None:
        try:
            while True:
                await asyncio.sleep(self._cleanup_interval)
                self._registry.prune()
        finally:
            if not self._closing:
                self._accepting = False
