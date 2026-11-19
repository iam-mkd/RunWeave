import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from math import inf

RUN_DEADLINE = "RUN_DEADLINE_EXCEEDED"
STEP_DEADLINE = "STEP_DEADLINE_EXCEEDED"


class DeadlineExceeded(TimeoutError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass
class Budget:
    expires_at: float
    code: str
    timeout: asyncio.Timeout | None = field(default=None, repr=False)
    # Called with the seconds a pause added, so run-bound resources can follow the deadline.
    listeners: list[Callable[[float], None]] = field(default_factory=list, repr=False)
    _paused: tuple[float, float] | None = field(default=None, repr=False)  # (left, original)

    def pause(self, until: float) -> None:
        """Stop the clock while waiting on a person; the wait itself is bounded by `until`.

        Pausing an already paused deadline only extends the bound. A deadline that has
        already passed cannot be paused.
        """
        if self.timeout is None:
            return
        now = asyncio.get_running_loop().time()
        if self._paused is None:
            left = self.expires_at - now
            if left <= 0:
                raise DeadlineExceeded(self.code)
            self._paused = (left, self.expires_at)
        left, _ = self._paused
        self.expires_at = max(self.expires_at, until + left)
        if not self.timeout.expired():
            self.timeout.reschedule(self.expires_at)

    def resume(self) -> float:
        """Restart the clock with the time that was left; returns how far the deadline moved."""
        if self.timeout is None or self._paused is None:
            return 0.0
        left, original = self._paused
        self._paused = None
        self.expires_at = asyncio.get_running_loop().time() + left
        if not self.timeout.expired():  # an overrun wait is reported by the scope
            self.timeout.reschedule(self.expires_at)
        shift = self.expires_at - original
        for listener in self.listeners:
            listener(shift)
        return shift


_budget: ContextVar[Budget | None] = ContextVar("deadline_budget", default=None)


def current_deadline() -> Budget | None:
    return _budget.get()


def remaining_budget() -> float:
    budget = _budget.get()
    return inf if budget is None else budget.expires_at - asyncio.get_running_loop().time()


def check_budget() -> None:
    task = asyncio.current_task()
    if task is not None and task.cancelling():
        raise asyncio.CancelledError
    budget = _budget.get()
    if budget is not None and remaining_budget() <= 0:
        raise DeadlineExceeded(budget.code)


@asynccontextmanager
async def deadline_scope(expires_at: float, code: str) -> AsyncIterator[None]:
    parent = _budget.get()
    if parent is not None and parent.expires_at <= expires_at:
        check_budget()
        yield
        check_budget()
        return

    timeout = asyncio.timeout_at(expires_at)
    token = _budget.set(Budget(expires_at, code, timeout))
    try:
        check_budget()
        async with timeout:
            yield
        check_budget()
    except Exception as exc:
        if timeout.expired() or remaining_budget() <= 0:
            raise DeadlineExceeded(code) from exc
        raise
    finally:
        _budget.reset(token)


@asynccontextmanager
async def paused_deadline(until: float) -> AsyncIterator[None]:
    """Pause the current run deadline for the duration of a wait bounded by `until`."""
    budget = _budget.get()
    if budget is None:
        yield
        return
    budget.pause(until)
    try:
        yield
    finally:
        budget.resume()
