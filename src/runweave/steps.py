"""Per-run step timeline shared by SDK agents and the built-in triage agent."""

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager, suppress
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

StepStatus = Literal["running", "succeeded", "failed"]
TITLE_LIMIT = 120
DETAIL_LIMIT = 1000
STEP_EVENT_CAP = 200


class StepEvent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    index: int = Field(ge=0)
    attempt: int = Field(default=1, ge=1)
    title: str = Field(min_length=1, max_length=TITLE_LIMIT)
    status: StepStatus
    detail: str | None = Field(default=None, max_length=DETAIL_LIMIT)
    started_at: datetime
    finished_at: datetime | None = None
    duration_ms: float | None = Field(default=None, ge=0)
    kind: Literal["step", "model_call"] = "step"
    fault: str | None = Field(default=None, max_length=40)


class StepLog(Protocol):
    """Records events for one run. Storage is capped; events past the cap are dropped."""

    async def begin(self, title: str, detail: str | None) -> StepEvent | None: ...
    async def end(self, event: StepEvent) -> None: ...


def finished_event(event: StepEvent, status: StepStatus) -> StepEvent:
    finished_at = datetime.now(UTC)
    return event.model_copy(
        update={
            "status": status,
            "finished_at": finished_at,
            "duration_ms": max(0.0, (finished_at - event.started_at).total_seconds() * 1000),
        }
    )


def clip(text: str | None, limit: int) -> str | None:
    if text is None:
        return None
    text = text.strip()
    return (text[: limit - 1] + "…" if len(text) > limit else text) or None


_log: ContextVar[StepLog | None] = ContextVar("runweave_steps", default=None)


@contextmanager
def step_scope(log: StepLog | None) -> Iterator[None]:
    token = _log.set(log)
    try:
        yield
    finally:
        _log.reset(token)


async def record_finished_step(
    title: str, detail: str | None, status: StepStatus, duration_ms: float
) -> None:
    """Add a step that already ran elsewhere, such as inside an HTTP agent."""
    log = _log.get()
    if log is None:
        return
    with suppress(Exception):
        event = await log.begin(clip(title, TITLE_LIMIT) or "Step", clip(detail, DETAIL_LIMIT))
        if event is not None:
            finished_at = datetime.now(UTC)
            await log.end(
                event.model_copy(
                    update={
                        "status": status,
                        "started_at": finished_at - timedelta(milliseconds=duration_ms),
                        "finished_at": finished_at,
                        "duration_ms": duration_ms,
                    }
                )
            )


@asynccontextmanager
async def record_step(title: str, detail: str | None = None) -> AsyncIterator[None]:
    log = _log.get()
    event = (
        None
        if log is None
        else await log.begin(clip(title, TITLE_LIMIT) or "Step", clip(detail, DETAIL_LIMIT))
    )
    status: StepStatus = "failed"
    try:
        yield
        status = "succeeded"
    finally:
        if log is not None and event is not None:
            # The timeline is observability, never a reason to fail or mask the run outcome.
            with suppress(Exception):
                await log.end(finished_event(event, status))
