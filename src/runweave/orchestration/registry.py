import asyncio
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from time import monotonic
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel

from runweave.agents.catalog import TRIAGE_AGENT
from runweave.budgets import BudgetPolicy, BudgetState, MemoryBudget
from runweave.orchestration.models import RunError, RunSnapshot, RunStatus, RunSummary
from runweave.security import LOCAL_PRINCIPAL, Principal
from runweave.steps import STEP_EVENT_CAP, StepEvent
from runweave.telemetry import Telemetry


@dataclass
class RunEntry:
    input: BaseModel
    snapshot: RunSnapshot
    admitted_at: float
    budget: MemoryBudget
    changed: asyncio.Event = field(default_factory=asyncio.Event)


class MemoryStepLog:
    def __init__(self, registry: "RunRegistry", run_id: UUID) -> None:
        self._registry = registry
        self._run_id = run_id

    async def begin(self, title: str, detail: str | None) -> StepEvent | None:
        return self._registry.begin_step(self._run_id, title, detail)

    async def end(self, event: StepEvent) -> None:
        self._registry.update_step(self._run_id, event)


class RunRegistry:
    """Single-event-loop state; synchronous operations never yield between transitions."""

    def __init__(
        self,
        max_history: int,
        ttl_seconds: float,
        clock: Callable[[], float] = monotonic,
        *,
        telemetry: Telemetry | None = None,
    ) -> None:
        if max_history < 1 or ttl_seconds <= 0:
            raise ValueError("History limits must be positive")
        self._max_history = max_history
        self._ttl = ttl_seconds
        self._clock = clock
        self._entries: dict[UUID, RunEntry] = {}
        self._finished: OrderedDict[UUID, float] = OrderedDict()
        self.telemetry = telemetry or Telemetry()

    def __len__(self) -> int:
        return len(self._entries)

    def unfinished_for_tenant(self, tenant: str) -> int:
        return sum(
            entry.snapshot.principal.tenant == tenant and not entry.snapshot.status.terminal
            for entry in self._entries.values()
        )

    def create(
        self,
        input: BaseModel,
        principal: Principal = LOCAL_PRINCIPAL,
        budget_policy: BudgetPolicy | None = None,
        *,
        agent: str = TRIAGE_AGENT,
        agent_version: str | None = None,
    ) -> RunSnapshot:
        self.prune()
        run_id = uuid4()
        budget = MemoryBudget(BudgetState(policy=budget_policy or BudgetPolicy()))
        snapshot = RunSnapshot(
            id=run_id,
            agent=agent,
            agent_version=agent_version,
            status=RunStatus.QUEUED,
            status_url=f"/v1/runs/{run_id}",
            submitted_at=datetime.now(UTC),
            principal=principal,
            usage=budget.state.summary(),
        )
        self._entries[run_id] = RunEntry(input, snapshot, self._clock(), budget)
        return snapshot

    def get(self, run_id: UUID) -> RunSnapshot | None:
        self.prune()
        entry = self._entries.get(run_id)
        if entry is not None:
            entry.snapshot = entry.snapshot.model_copy(
                update={"usage": entry.budget.state.summary()}
            )
        return entry.snapshot if entry else None

    def list(
        self,
        principal: Principal,
        *,
        agent: str | None = None,
        status: RunStatus | None = None,
        limit: int = 50,
        before: datetime | None = None,
    ) -> list[RunSummary]:
        self.prune()
        matches = [
            entry.snapshot
            for entry in self._entries.values()
            if principal.owns(entry.snapshot.principal)
            and (agent is None or entry.snapshot.agent == agent)
            and (status is None or entry.snapshot.status == status)
            and (before is None or entry.snapshot.submitted_at < before)
        ]
        matches.sort(key=lambda snapshot: (snapshot.submitted_at, str(snapshot.id)), reverse=True)
        return [snapshot.summary() for snapshot in matches[:limit]]

    def input(self, run_id: UUID) -> BaseModel | None:
        entry = self._entries.get(run_id)
        return entry.input if entry is not None else None

    def budget(self, run_id: UUID) -> MemoryBudget:
        return self._entries[run_id].budget

    def step_log(self, run_id: UUID) -> MemoryStepLog:
        return MemoryStepLog(self, run_id)

    def change_event(self, run_id: UUID) -> asyncio.Event | None:
        """Set on the next change. Take it before reading a snapshot so no update is missed."""
        entry = self._entries.get(run_id)
        return entry.changed if entry is not None else None

    def _update(self, entry: RunEntry, **changes: Any) -> None:
        entry.snapshot = entry.snapshot.model_copy(update=changes)
        entry.changed.set()
        entry.changed = asyncio.Event()

    def begin_step(self, run_id: UUID, title: str, detail: str | None) -> StepEvent | None:
        entry = self._entries.get(run_id)
        if (
            entry is None
            or entry.snapshot.status.terminal
            or len(entry.snapshot.steps) >= STEP_EVENT_CAP
        ):
            return None
        event = StepEvent(
            index=len(entry.snapshot.steps),
            title=title,
            detail=detail,
            status="running",
            started_at=datetime.now(UTC),
        )
        self._update(entry, steps=(*entry.snapshot.steps, event))
        return event

    def update_step(self, run_id: UUID, event: StepEvent) -> None:
        entry = self._entries.get(run_id)
        if entry is None or event.index >= len(entry.snapshot.steps):
            return
        steps = list(entry.snapshot.steps)
        steps[event.index] = event
        self._update(entry, steps=tuple(steps))

    def claim(self, run_id: UUID) -> BaseModel | None:
        entry = self._entries.get(run_id)
        if entry is None or entry.snapshot.status != RunStatus.QUEUED:
            return None
        self._update(entry, status=RunStatus.RUNNING, started_at=datetime.now(UTC))
        self.telemetry.queue_wait.observe(self._clock() - entry.admitted_at)
        return entry.input

    def finish(
        self,
        run_id: UUID,
        status: RunStatus,
        *,
        result: BaseModel | dict[str, Any] | None = None,
        error: RunError | None = None,
    ) -> RunSnapshot | None:
        entry = self._entries.get(run_id)
        if entry is None or entry.snapshot.status.terminal:
            return entry.snapshot if entry else None
        if not status.terminal:
            raise ValueError("finish requires a terminal state")
        if entry.snapshot.status == RunStatus.QUEUED and status not in (
            RunStatus.CANCELLED,
            RunStatus.TIMED_OUT,
        ):
            raise ValueError("Queued work can only start, be cancelled, or time out")
        if (status == RunStatus.SUCCEEDED) != (result is not None):
            raise ValueError("Only successful runs must contain a result")
        if (status in (RunStatus.FAILED, RunStatus.TIMED_OUT)) != (error is not None):
            raise ValueError("Only failed or timed-out runs must contain an error")
        self._update(
            entry,
            status=status,
            finished_at=datetime.now(UTC),
            result=result.model_dump(mode="json") if isinstance(result, BaseModel) else result,
            error=error,
            usage=entry.budget.state.summary(),
        )
        self._finished[run_id] = self._clock()
        snapshot = entry.snapshot
        self.telemetry.finished(snapshot, self._finished[run_id] - entry.admitted_at)
        self.prune()
        return snapshot

    def cancel_unfinished(self) -> None:
        for run_id in list(self._entries):
            entry = self._entries.get(run_id)
            if entry is not None and not entry.snapshot.status.terminal:
                self.finish(run_id, RunStatus.CANCELLED)

    def prune(self) -> None:
        now = self._clock()
        while self._finished:
            run_id, finished = next(iter(self._finished.items()))
            if len(self._finished) <= self._max_history and now - finished < self._ttl:
                break
            self._finished.popitem(last=False)
            entry = self._entries.pop(run_id, None)
            if entry is not None:
                entry.changed.set()
