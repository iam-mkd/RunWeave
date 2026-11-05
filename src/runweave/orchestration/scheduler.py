import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID

from runweave.agents.contracts import AgentExecutor, IncidentRequest
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
    ) -> None:
        if worker_count < 1 or queue_capacity < 1 or cleanup_interval <= 0:
            raise ValueError("Scheduler limits must be positive")
        self._executor = executor
        self._registry = registry
        self._worker_count = worker_count
        self._cleanup_interval = cleanup_interval
        self._queue: asyncio.Queue[UUID] = asyncio.Queue(maxsize=queue_capacity)
        self._active: dict[UUID, asyncio.Task[None]] = {}
        self._accepting = False
        self._closing = False
        self._started = False

    @property
    def ready(self) -> bool:
        return self._accepting

    def submit(self, incident: IncidentRequest) -> RunSnapshot:
        if not self.ready:
            raise AdmissionRejected("SCHEDULER_UNAVAILABLE")
        if self._queue.full():
            raise AdmissionRejected("QUEUE_FULL")
        # No await between capacity check, record creation, and enqueue.
        snapshot = self._registry.create(incident)
        self._queue.put_nowait(snapshot.id)
        return snapshot

    def get(self, run_id: UUID) -> RunSnapshot | None:
        return self._registry.get(run_id)

    def cancel(self, run_id: UUID) -> RunSnapshot | None:
        snapshot = self.get(run_id)
        if snapshot is None or snapshot.status.terminal:
            return snapshot
        cancelled = self._registry.finish(run_id, RunStatus.CANCELLED)
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
            self._accepting = True
            try:
                yield
            finally:
                self._accepting = False
                self._closing = True
                self._registry.cancel_unfinished()
                for task in [*workers, cleaner]:
                    task.cancel()
                while not self._queue.empty():
                    self._queue.get_nowait()
                    self._queue.task_done()

    async def _worker(self) -> None:
        try:
            while True:
                run_id = await self._queue.get()
                try:
                    incident = self._registry.claim(run_id)
                    if incident is None:
                        continue
                    task = asyncio.create_task(
                        self._execute(run_id, incident), name=f"runweave-run-{run_id}"
                    )
                    self._active[run_id] = task
                    try:
                        await task
                    except asyncio.CancelledError:
                        worker = asyncio.current_task()
                        if worker is not None and worker.cancelling():
                            raise
                    finally:
                        self._active.pop(run_id, None)
                finally:
                    self._queue.task_done()
        finally:
            if not self._closing:
                self._accepting = False

    async def _execute(self, run_id: UUID, incident: IncidentRequest) -> None:
        try:
            result = await self._executor.run(incident)
        except asyncio.CancelledError:
            self._registry.finish(run_id, RunStatus.CANCELLED)
            raise
        except Exception:
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
