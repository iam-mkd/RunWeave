from collections.abc import AsyncIterator
from datetime import datetime
from typing import Protocol
from uuid import UUID

from runweave.agents.catalog import AgentCatalog
from runweave.orchestration.admission import Submission
from runweave.orchestration.models import RunSnapshot, RunStatus, RunSummary
from runweave.orchestration.scheduler import Scheduler
from runweave.security import LOCAL_PRINCIPAL, Principal


class RunBackend(Protocol):
    @property
    def ready(self) -> bool: ...
    @property
    def agents(self) -> AgentCatalog: ...

    async def submit(
        self, submission: Submission, principal: Principal = LOCAL_PRINCIPAL
    ) -> RunSnapshot: ...
    async def get(self, run_id: UUID, principal: Principal | None = None) -> RunSnapshot | None: ...
    async def list(
        self,
        principal: Principal,
        *,
        agent: str | None = None,
        status: RunStatus | None = None,
        limit: int = 50,
        before: datetime | None = None,
    ) -> list[RunSummary]: ...
    def watch(
        self, run_id: UUID, principal: Principal, *, heartbeat: float
    ) -> AsyncIterator[RunSnapshot | None]: ...
    async def cancel(
        self, run_id: UUID, principal: Principal | None = None
    ) -> RunSnapshot | None: ...
    async def metrics(self) -> str: ...
    async def join(self) -> None: ...


class MemoryBackend:
    def __init__(self, scheduler: Scheduler) -> None:
        self._scheduler = scheduler

    @property
    def ready(self) -> bool:
        return self._scheduler.ready

    @property
    def agents(self) -> AgentCatalog:
        return self._scheduler.agents

    async def submit(
        self, submission: Submission, principal: Principal = LOCAL_PRINCIPAL
    ) -> RunSnapshot:
        return self._scheduler.submit(submission, principal)

    async def get(self, run_id: UUID, principal: Principal | None = None) -> RunSnapshot | None:
        return self._scheduler.get(run_id, principal)

    async def list(
        self,
        principal: Principal,
        *,
        agent: str | None = None,
        status: RunStatus | None = None,
        limit: int = 50,
        before: datetime | None = None,
    ) -> list[RunSummary]:
        return self._scheduler.list(
            principal, agent=agent, status=status, limit=limit, before=before
        )

    def watch(
        self, run_id: UUID, principal: Principal, *, heartbeat: float
    ) -> AsyncIterator[RunSnapshot | None]:
        return self._scheduler.watch(run_id, principal, heartbeat=heartbeat)

    async def cancel(self, run_id: UUID, principal: Principal | None = None) -> RunSnapshot | None:
        return self._scheduler.cancel(run_id, principal)

    async def metrics(self) -> str:
        return self._scheduler.metrics()

    async def join(self) -> None:
        await self._scheduler.join()
