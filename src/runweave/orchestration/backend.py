from typing import Protocol
from uuid import UUID

from runweave.agents.contracts import IncidentRequest
from runweave.orchestration.models import RunSnapshot
from runweave.orchestration.scheduler import Scheduler


class RunBackend(Protocol):
    @property
    def ready(self) -> bool: ...

    async def submit(self, incident: IncidentRequest) -> RunSnapshot: ...
    async def get(self, run_id: UUID) -> RunSnapshot | None: ...
    async def cancel(self, run_id: UUID) -> RunSnapshot | None: ...
    async def metrics(self) -> str: ...
    async def join(self) -> None: ...


class MemoryBackend:
    def __init__(self, scheduler: Scheduler) -> None:
        self._scheduler = scheduler

    @property
    def ready(self) -> bool:
        return self._scheduler.ready

    async def submit(self, incident: IncidentRequest) -> RunSnapshot:
        return self._scheduler.submit(incident)

    async def get(self, run_id: UUID) -> RunSnapshot | None:
        return self._scheduler.get(run_id)

    async def cancel(self, run_id: UUID) -> RunSnapshot | None:
        return self._scheduler.cancel(run_id)

    async def metrics(self) -> str:
        return self._scheduler.metrics()

    async def join(self) -> None:
        await self._scheduler.join()
