import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from uuid import UUID

from runweave.agents.contracts import AgentExecutor, IncidentRequest, IncidentResult
from runweave.approvals import AwaitingApproval, ticket_scope
from runweave.budgets import BudgetExceeded, budget_scope
from runweave.checkpoints import CheckpointError, checkpoint_scope
from runweave.core.settings import Settings
from runweave.deadline import RUN_DEADLINE, DeadlineExceeded, deadline_scope
from runweave.identity import IdentityProvider
from runweave.orchestration.models import RunError, RunSnapshot, RunStatus
from runweave.orchestration.scheduler import AdmissionRejected
from runweave.security import LOCAL_PRINCIPAL, ContentPolicy, Principal, principal_scope
from runweave.storage.engine import StorageUnavailable, storage_errors
from runweave.storage.postgres import Claim, PostgresRuns
from runweave.telemetry import Telemetry


class DurableScheduler:
    def __init__(
        self,
        store: PostgresRuns,
        executor: AgentExecutor,
        settings: Settings,
        telemetry: Telemetry,
        content_policy: ContentPolicy | None = None,
    ) -> None:
        self._store = store
        self._executor = executor
        self._settings = settings
        self._telemetry = telemetry
        self._active: dict[UUID, tuple[UUID, asyncio.Task[IncidentResult]]] = {}
        self._accepting = False
        self._stopping = asyncio.Event()
        self._idle = asyncio.Event()
        self._idle.set()
        self._in_progress = 0
        self._content_policy = content_policy or ContentPolicy()
        self.approvals = store.approvals()
        self._identity = IdentityProvider(settings)

    @property
    def ready(self) -> bool:
        return self._accepting

    async def submit(
        self, incident: IncidentRequest, principal: Principal = LOCAL_PRINCIPAL
    ) -> RunSnapshot:
        principal.require("runs:submit")
        if incident.create_ticket:
            principal.require("tools:create_ticket")
            try:
                import runweave.agents.ticket  # noqa: F401
            except ModuleNotFoundError as exc:
                if exc.name != "pydantic_ai":
                    raise
                raise AdmissionRejected("WORKFLOW_EXTRA_REQUIRED") from None
        self._content_policy.check(incident.model_dump_json())
        if not self.ready:
            raise AdmissionRejected("SCHEDULER_UNAVAILABLE")
        with storage_errors():
            run = await self._store.admit(
                incident,
                capacity=self._settings.queue_capacity,
                run_seconds=self._settings.run_timeout_seconds,
                attempts=self._settings.durable_max_attempts,
                history_max=self._settings.history_max_records,
                principal=principal,
                tenant_limit=self._settings.tenant_max_unfinished,
                budget_policy=self._settings.budget_policy(),
                workflow=self._settings.agent_workflow,
                max_steps=self._settings.agent_max_steps,
            )
        if run is None:
            self._telemetry.rejected["queue_full"] += 1
            raise AdmissionRejected("QUEUE_FULL")
        self._telemetry.admitted += 1
        return run

    async def get(self, run_id: UUID, principal: Principal | None = None) -> RunSnapshot | None:
        if principal is not None:
            principal.require("runs:read")
        with storage_errors():
            return await self._store.get(run_id, principal)

    async def cancel(self, run_id: UUID, principal: Principal | None = None) -> RunSnapshot | None:
        if principal is not None:
            principal.require("runs:cancel")
        with storage_errors():
            run = await self._store.cancel(run_id, principal)
        if run is not None and run.status == RunStatus.CANCELLED:
            for active_id, task in self._active.values():
                if active_id == run_id and not task.cancelling():
                    task.cancel()
        return run

    async def metrics(self) -> str:
        with storage_errors():
            queued, retained = await self._store.counts()
        return self._telemetry.render(queued, len(self._active), retained)

    async def join(self) -> None:
        await self._idle.wait()

    async def _pause(self) -> None:
        with suppress(TimeoutError):
            await asyncio.wait_for(self._stopping.wait(), self._settings.db_poll_seconds)

    @asynccontextmanager
    async def running(self) -> AsyncIterator[None]:
        try:
            with storage_errors():
                await self._store.check()
        except StorageUnavailable:
            raise RuntimeError(
                "PostgreSQL is unavailable or the RunWeave migration is missing"
            ) from None
        async with asyncio.TaskGroup() as group:
            workers = [
                group.create_task(self._worker()) for _ in range(self._settings.worker_count)
            ]
            maintenance = group.create_task(self._maintain())
            lag = group.create_task(self._telemetry.sample_loop_lag())
            self._accepting = True
            try:
                yield
                self._accepting = False
                self._stopping.set()
                with suppress(TimeoutError):
                    async with asyncio.timeout(self._settings.shutdown_grace_seconds):
                        await self.join()
            finally:
                self._accepting = False
                self._stopping.set()
                for task in (*workers, maintenance, lag):
                    if not task.cancelling():
                        task.cancel()

    async def _maintain(self) -> None:
        try:
            while not self._stopping.is_set():
                with storage_errors():
                    await self._store.maintain(
                        retry_delay=self._settings.durable_retry_delay_seconds,
                        history_ttl=self._settings.history_ttl_seconds,
                        history_max=self._settings.history_max_records,
                    )
                await self._pause()
        except StorageUnavailable:
            self._accepting = False
            raise RuntimeError("Durable run maintenance failed") from None

    async def _worker(self) -> None:
        try:
            while not self._stopping.is_set():
                self._in_progress += 1
                self._idle.clear()
                try:
                    loop = asyncio.get_running_loop()
                    before_claim = loop.time()
                    with storage_errors():
                        claim = await self._store.claim(self._settings.lease_seconds)
                        if claim is not None:
                            await self._execute(claim, before_claim + claim.remaining_seconds)
                finally:
                    self._in_progress -= 1
                    if not self._in_progress:
                        self._idle.set()
                if claim is None:
                    await self._pause()
        except StorageUnavailable:
            self._accepting = False
            raise RuntimeError("Durable run worker lost database access") from None

    async def _heartbeat(self, claim: Claim, task: asyncio.Task[IncidentResult]) -> None:
        while True:
            await asyncio.sleep(self._settings.lease_seconds / 3)
            if not await self._store.renew(claim, self._settings.lease_seconds):
                task.cancel()
                return

    async def _run(self, claim: Claim, deadline: float) -> IncidentResult:
        def authorize() -> Principal:
            return self._identity.execution_principal(claim.snapshot.principal)

        with (
            principal_scope(authorize()),
            budget_scope(self._store.ledger(claim)),
            checkpoint_scope(self._store.checkpoints(claim)),
            ticket_scope(
                self._store.ticket_gate(
                    claim,
                    ttl=self._settings.approval_ttl_seconds,
                    limit=self._settings.approval_max_pending,
                    authorize=authorize,
                    content_policy=self._content_policy,
                )
            ),
            self._telemetry.span("agent.run", run_id=claim.snapshot.id),
        ):
            async with deadline_scope(deadline, RUN_DEADLINE):
                claim.snapshot.principal.require("runs:submit")
                self._content_policy.check(claim.incident.model_dump_json())
                result = await self._executor.run(claim.incident)
                self._content_policy.check(result.model_dump_json())
                return result

    async def _execute(self, claim: Claim, deadline: float) -> None:
        run_id = claim.snapshot.id
        task = asyncio.create_task(self._run(claim, deadline))
        heartbeat = asyncio.create_task(self._heartbeat(claim, task))
        self._active[claim.token] = (run_id, task)
        self._telemetry.peak_runs = max(self._telemetry.peak_runs, len(self._active))
        if claim.snapshot.started_at is not None:
            self._telemetry.queue_wait.observe(
                (claim.snapshot.started_at - claim.snapshot.submitted_at).total_seconds()
            )
        try:
            await asyncio.wait((task, heartbeat), return_when=asyncio.FIRST_COMPLETED)
            if heartbeat.done():
                heartbeat.result()
                return
            if task.cancelled():
                return
            try:
                result = task.result()
            except AwaitingApproval:
                self._telemetry.audit("approval", "waiting", claim.snapshot.principal)
                return
            except DeadlineExceeded as exc:
                finished = await self._store.finish(
                    claim,
                    RunStatus.TIMED_OUT,
                    error=RunError(code=exc.code, message="Execution deadline exceeded"),
                )
            except Exception as exc:
                finished = await self._store.finish(
                    claim,
                    RunStatus.FAILED,
                    error=RunError(
                        code=exc.code
                        if isinstance(exc, (BudgetExceeded, CheckpointError))
                        else "RUN_FAILED",
                        message="Agent execution failed",
                    ),
                )
            else:
                finished = await self._store.finish(claim, RunStatus.SUCCEEDED, result=result)
            if finished is not None and finished.finished_at is not None:
                self._telemetry.finished(
                    finished, (finished.finished_at - finished.submitted_at).total_seconds()
                )
        finally:
            for child in (task, heartbeat):
                if not child.done() and not child.cancelling():
                    child.cancel()
            await asyncio.gather(task, heartbeat, return_exceptions=True)
            self._active.pop(claim.token, None)
