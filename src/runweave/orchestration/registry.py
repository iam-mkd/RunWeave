from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from time import monotonic
from uuid import UUID, uuid4

from runweave.agents.contracts import IncidentRequest, IncidentResult
from runweave.orchestration.models import RunError, RunSnapshot, RunStatus


@dataclass
class RunEntry:
    incident: IncidentRequest
    snapshot: RunSnapshot


class RunRegistry:
    """Single-event-loop state; synchronous operations never yield between transitions."""

    def __init__(
        self, max_history: int, ttl_seconds: float, clock: Callable[[], float] = monotonic
    ) -> None:
        if max_history < 1 or ttl_seconds <= 0:
            raise ValueError("History limits must be positive")
        self._max_history = max_history
        self._ttl = ttl_seconds
        self._clock = clock
        self._entries: dict[UUID, RunEntry] = {}
        self._finished: OrderedDict[UUID, float] = OrderedDict()

    def create(self, incident: IncidentRequest) -> RunSnapshot:
        self.prune()
        run_id = uuid4()
        snapshot = RunSnapshot(
            id=run_id,
            status=RunStatus.QUEUED,
            status_url=f"/v1/runs/{run_id}",
            submitted_at=datetime.now(UTC),
        )
        self._entries[run_id] = RunEntry(incident, snapshot)
        return snapshot

    def get(self, run_id: UUID) -> RunSnapshot | None:
        self.prune()
        entry = self._entries.get(run_id)
        return entry.snapshot if entry else None

    def claim(self, run_id: UUID) -> IncidentRequest | None:
        entry = self._entries.get(run_id)
        if entry is None or entry.snapshot.status != RunStatus.QUEUED:
            return None
        entry.snapshot = entry.snapshot.model_copy(
            update={"status": RunStatus.RUNNING, "started_at": datetime.now(UTC)}
        )
        return entry.incident

    def finish(
        self,
        run_id: UUID,
        status: RunStatus,
        *,
        result: IncidentResult | None = None,
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
        entry.snapshot = entry.snapshot.model_copy(
            update={
                "status": status,
                "finished_at": datetime.now(UTC),
                "result": result,
                "error": error,
            }
        )
        self._finished[run_id] = self._clock()
        snapshot = entry.snapshot
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
            self._entries.pop(run_id, None)
