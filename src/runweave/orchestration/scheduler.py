import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from datetime import datetime
from uuid import UUID

import httpx

from runweave.agents.catalog import AgentCatalog
from runweave.agents.contracts import AgentExecutor, IncidentRequest
from runweave.budgets import BudgetExceeded, BudgetPolicy, budget_scope
from runweave.deadline import RUN_DEADLINE, DeadlineExceeded, deadline_scope
from runweave.orchestration.admission import (
    AdmissionRejected,
    Submission,
    input_json,
    output_json,
    resolve,
)
from runweave.orchestration.models import RunError, RunSnapshot, RunStatus, RunSummary
from runweave.orchestration.registry import RunRegistry
from runweave.proxy.bindings import ProxyBindings, bind_run
from runweave.sdk import AgentError, RunContext
from runweave.security import (
    LOCAL_PRINCIPAL,
    ContentPolicy,
    Principal,
    TenantQuotaExceeded,
    principal_scope,
)
from runweave.steps import step_scope

__all__ = ["AdmissionRejected", "Scheduler"]


def failure_code(exc: Exception) -> str:
    # Only RunWeave-classified errors expose a code; arbitrary agent exceptions never do.
    return exc.code if isinstance(exc, (BudgetExceeded, AgentError)) else "RUN_FAILED"


class Scheduler:
    def __init__(
        self,
        agents: AgentCatalog | AgentExecutor,
        registry: RunRegistry,
        *,
        worker_count: int,
        queue_capacity: int,
        cleanup_interval: float,
        run_timeout: float = 30.0,
        shutdown_grace: float = 5.0,
        tenant_limit: int = 1000,
        content_policy: ContentPolicy | None = None,
        budget_policy: BudgetPolicy | Callable[[], BudgetPolicy] | None = None,
        output_max_bytes: int = 65536,
        proxy: ProxyBindings | None = None,
    ) -> None:
        if worker_count < 1 or queue_capacity < 1 or cleanup_interval <= 0 or tenant_limit < 1:
            raise ValueError("Scheduler limits must be positive")
        if run_timeout <= 0 or shutdown_grace < 0:
            raise ValueError("Run timeout must be positive and shutdown grace nonnegative")
        self._agents = (
            agents
            if isinstance(agents, AgentCatalog)
            else AgentCatalog.with_triage(agents, version="custom", max_steps=200)
        )
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
        self._tenant_limit = tenant_limit
        self._content_policy = content_policy or ContentPolicy()
        # A callable lets Settings change the policy for runs admitted afterwards.
        policy = budget_policy or BudgetPolicy()
        self._budget_policy: Callable[[], BudgetPolicy] = (
            policy if callable(policy) else (lambda: policy)
        )
        self._output_max_bytes = output_max_bytes
        self._proxy = proxy

    @property
    def ready(self) -> bool:
        return self._accepting

    @property
    def agents(self) -> AgentCatalog:
        return self._agents

    def submit(self, submission: Submission, principal: Principal = LOCAL_PRINCIPAL) -> RunSnapshot:
        principal.require("runs:submit")
        definition, parsed = resolve(self._agents, submission)
        if isinstance(parsed, IncidentRequest):
            if parsed.memory is not None:
                raise AdmissionRejected("MEMORY_REQUIRES_POSTGRES")
            if parsed.create_ticket:
                raise AdmissionRejected("DURABLE_APPROVAL_REQUIRED")
        self._content_policy.check(input_json(parsed))
        if not self.ready:
            self._telemetry.rejected["unavailable"] += 1
            raise AdmissionRejected("SCHEDULER_UNAVAILABLE")
        if self._registry.unfinished_for_tenant(principal.tenant) >= self._tenant_limit:
            raise TenantQuotaExceeded()
        if self._queue.full():
            self._telemetry.rejected["queue_full"] += 1
            raise AdmissionRejected("QUEUE_FULL")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._run_timeout
        # No await between capacity check, record creation, and enqueue.
        snapshot = self._registry.create(
            parsed,
            principal,
            self._budget_policy(),
            agent=definition.info.name,
            agent_version=definition.info.version,
        )
        self._queue.put_nowait(snapshot.id)
        self._queued_deadlines[snapshot.id] = loop.call_at(
            deadline, self._expire_queued, snapshot.id
        )
        self._telemetry.admitted += 1
        return snapshot

    def metrics(self) -> str:
        return self._telemetry.render(self._queue.qsize(), len(self._active), len(self._registry))

    def get(self, run_id: UUID, principal: Principal | None = None) -> RunSnapshot | None:
        if principal is not None:
            principal.require("runs:read")
        snapshot = self._registry.get(run_id)
        return (
            snapshot
            if snapshot is None or principal is None or principal.owns(snapshot.principal)
            else None
        )

    def list(
        self,
        principal: Principal,
        *,
        agent: str | None = None,
        status: RunStatus | None = None,
        limit: int = 50,
        before: datetime | None = None,
    ) -> list[RunSummary]:
        principal.require("runs:read")
        return self._registry.list(
            principal, agent=agent, status=status, limit=limit, before=before
        )

    async def watch(
        self, run_id: UUID, principal: Principal, *, heartbeat: float
    ) -> AsyncIterator[RunSnapshot | None]:
        """Yield the snapshot, then each change until terminal; None marks a quiet interval."""
        while True:
            changed = self._registry.change_event(run_id)
            snapshot = self.get(run_id, principal)
            if snapshot is None or changed is None:
                return
            yield snapshot
            if snapshot.status.terminal:
                return
            while True:
                try:
                    async with asyncio.timeout(heartbeat):
                        await changed.wait()
                    break
                except TimeoutError:
                    yield None

    def cancel(self, run_id: UUID, principal: Principal | None = None) -> RunSnapshot | None:
        if principal is not None:
            principal.require("runs:cancel")
        snapshot = self._registry.get(run_id)
        if (
            snapshot is not None
            and principal is not None
            and not principal.owns(snapshot.principal)
        ):
            return None
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
                    if self._registry.claim(run_id) is None:
                        continue
                    task = asyncio.create_task(
                        self._execute(run_id, deadline), name=f"runweave-run-{run_id}"
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

    async def _execute(self, run_id: UUID, deadline: float) -> None:
        try:
            snapshot = self._registry.get(run_id)
            parsed = self._registry.input(run_id)
            assert snapshot is not None and parsed is not None
            definition = self._agents.get(snapshot.agent)
            if definition is None:
                raise AdmissionRejected("AGENT_UNAVAILABLE")
            ledger = self._registry.budget(run_id)
            steps = self._registry.step_log(run_id)
            with (
                principal_scope(snapshot.principal),
                budget_scope(ledger),
                step_scope(steps),
                bind_run(
                    self._proxy,
                    run_id=run_id,
                    attempt=1,
                    agent=snapshot.agent,
                    ledger=ledger,
                    steps=steps,
                    expires_at=deadline,
                ) as proxy,
                self._telemetry.span("agent.run", run_id=run_id),
            ):
                context = RunContext(
                    run_id=run_id,
                    agent=snapshot.agent,
                    max_steps=definition.info.max_steps,
                    content_policy=self._content_policy,
                    proxy=proxy,
                )
                async with deadline_scope(deadline, RUN_DEADLINE):
                    snapshot.principal.require("runs:submit")
                    self._content_policy.check(input_json(parsed))
                    output = await definition.run(parsed, context)
                    result = output_json(
                        output, max_bytes=self._output_max_bytes, policy=self._content_policy
                    )
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
            code = exc.code if isinstance(exc, AdmissionRejected) else failure_code(exc)
            self._registry.finish(
                run_id,
                RunStatus.FAILED,
                error=RunError(code=code, message="Agent execution failed"),
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
