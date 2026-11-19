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
    """Stop the run deadline's clock while waiting (for a human decision), until `until`.

    The time left when the pause starts is kept and resumes when it ends; the wait itself is
    bounded by `until` instead. Listeners learn how far the deadline moved.
    """
    budget = _budget.get()
    if budget is None or budget.timeout is None:
        yield
        return
    loop = asyncio.get_running_loop()
    started = loop.time()
    remaining = budget.expires_at - started
    if remaining <= 0:
        raise DeadlineExceeded(budget.code)
    original = budget.expires_at
    budget.expires_at = max(original, until + remaining)
    budget.timeout.reschedule(budget.expires_at)
    try:
        yield
    finally:
        budget.expires_at = loop.time() + remaining
        if not budget.timeout.expired():  # an overrun wait is reported by the scope
            budget.timeout.reschedule(budget.expires_at)
        shift = budget.expires_at - original
        for listener in budget.listeners:
            listener(shift)
