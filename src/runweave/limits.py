"""Per-provider call limits: a semaphore bounds in-flight calls, a pacer bounds calls per minute.

Both waits consume the caller's run deadline; neither holds a lock across the call itself.
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from runweave.deadline import check_budget


class ProviderLimiter:
    def __init__(self, *, concurrency: int, requests_per_minute: int) -> None:
        if concurrency < 1 or requests_per_minute < 1:
            raise ValueError("Provider limits must be positive")
        self._slots = asyncio.Semaphore(concurrency)
        self._pace = asyncio.Lock()
        self._interval = 60 / requests_per_minute
        self._next_start = 0.0

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        async with self._slots:
            async with self._pace:
                loop = asyncio.get_running_loop()
                delay = self._next_start - loop.time()
                if delay > 0:
                    await asyncio.sleep(delay)
                check_budget()
                self._next_start = loop.time() + self._interval
            yield
