from typing import Protocol
from uuid import UUID

from runweave.agents.contracts import IncidentRequest
from runweave.orchestration.models import RunSnapshot
from runweave.orchestration.scheduler import Scheduler
from runweave.security import LOCAL_PRINCIPAL, Principal


class RunBackend(Protocol):
    @property
    def ready(self) -> bool: ...

    async def submit(
        self, incident: IncidentRequest, principal: Principal = LOCAL_PRINCIPAL
    ) -> RunSnapshot: ...
    async def get(self, run_id: UUID, principal: Principal | None = None) -> RunSnapshot | None: ...
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

    async def submit(
        self, incident: IncidentRequest, principal: Principal = LOCAL_PRINCIPAL
    ) -> RunSnapshot:
        return self._scheduler.submit(incident, principal)

    async def get(self, run_id: UUID, principal: Principal | None = None) -> RunSnapshot | None:
        return self._scheduler.get(run_id, principal)

    async def cancel(self, run_id: UUID, principal: Principal | None = None) -> RunSnapshot | None:
        return self._scheduler.cancel(run_id, principal)

    async def metrics(self) -> str:
        return self._scheduler.metrics()

    async def join(self) -> None:
        await self._scheduler.join()
