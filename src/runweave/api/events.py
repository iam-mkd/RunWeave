"""Bounded server-sent event streams for run snapshots."""

import asyncio
from collections.abc import AsyncIterator, Callable
from uuid import UUID

from runweave.orchestration.backend import RunBackend
from runweave.security import Principal


class StreamLease:
    def __init__(self, release: Callable[[], None]) -> None:
        self._release: Callable[[], None] | None = release

    def release(self) -> None:
        if self._release is not None:
            self._release()
            self._release = None


class EventStreams:
    def __init__(self, *, max_subscribers: int, max_seconds: float, heartbeat: float) -> None:
        self._max = max_subscribers
        self._open = 0
        self.max_seconds = max_seconds
        self.heartbeat = heartbeat

    def acquire(self) -> StreamLease | None:
        if self._open >= self._max:
            return None
        self._open += 1
        return StreamLease(self._done)

    def _done(self) -> None:
        self._open -= 1

    async def frames(
        self,
        backend: RunBackend,
        run_id: UUID,
        principal: Principal,
        lease: StreamLease,
        check: Callable[[str], None],
    ) -> AsyncIterator[str]:
        try:
            # Streams end on their own so clients reconnect and shutdown is never held open.
            async with asyncio.timeout(self.max_seconds):
                async for snapshot in backend.watch(run_id, principal, heartbeat=self.heartbeat):
                    if snapshot is None:
                        yield ": keep-alive\n\n"
                        continue
                    payload = snapshot.model_dump_json()
                    check(payload)
                    yield f"event: snapshot\ndata: {payload}\n\n"
        except TimeoutError:
            yield "event: reconnect\ndata: {}\n\n"
        finally:
            lease.release()
