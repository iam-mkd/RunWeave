import asyncio
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from random import uniform

import httpx

from runweave.deadline import check_budget, remaining_budget
from runweave.telemetry import Telemetry
from runweave.tools.contracts import HealthObservation, HealthQuery, HealthTool

_RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}
_RETRYABLE_NETWORK = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadError,
    httpx.ReadTimeout,
    httpx.RemoteProtocolError,
)


def retry_after_seconds(value: str | None, now: datetime) -> float:
    if value is None:
        return 0.0
    value = value.strip()
    if value.isascii() and value.isdecimal():
        return float(value)
    try:
        retry_at = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=UTC)
    return max(0.0, (retry_at - now).total_seconds())


class RetryingHealthTool:
    """Opt-in retry wrapper for the read-only service health lookup."""

    def __init__(
        self,
        tool: HealthTool,
        *,
        attempts: int,
        base_delay: float,
        max_delay: float,
        telemetry: Telemetry | None = None,
    ) -> None:
        self._tool = tool
        self._attempts = attempts
        self._base_delay = base_delay
        self._max_delay = max_delay
        self._telemetry = telemetry

    async def execute(self, query: HealthQuery) -> HealthObservation:
        for attempt in range(self._attempts - 1):
            check_budget()
            try:
                return await self._tool.execute(query)
            except httpx.HTTPError as exc:
                if isinstance(exc, httpx.HTTPStatusError):
                    if exc.response.status_code not in _RETRYABLE_STATUS:
                        raise
                    retry_after = retry_after_seconds(
                        exc.response.headers.get("Retry-After"), datetime.now(UTC)
                    )
                elif isinstance(exc, _RETRYABLE_NETWORK):
                    retry_after = 0.0
                else:
                    raise
                check_budget()
                cap = min(self._max_delay, self._base_delay * 2**attempt)
                delay = max(retry_after, uniform(cap / 2, cap))
                if delay > self._max_delay or delay >= remaining_budget():
                    raise
                await asyncio.sleep(delay)
                check_budget()
                if self._telemetry:
                    self._telemetry.retry_attempts += 1

        check_budget()
        return await self._tool.execute(query)
