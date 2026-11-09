import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from math import inf

RUN_DEADLINE = "RUN_DEADLINE_EXCEEDED"
STEP_DEADLINE = "STEP_DEADLINE_EXCEEDED"


class DeadlineExceeded(TimeoutError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class Budget:
    expires_at: float
    code: str


_budget: ContextVar[Budget | None] = ContextVar("deadline_budget", default=None)


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

    token = _budget.set(Budget(expires_at, code))
    timeout = asyncio.timeout_at(expires_at)
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
