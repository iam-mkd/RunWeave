import asyncio
from datetime import UTC, datetime

import httpx
import pytest

from runweave.agents.contracts import IncidentRequest
from runweave.agents.fake import FakeAgentModel
from runweave.agents.runner import AgentRunner
from runweave.deadline import STEP_DEADLINE, deadline_scope
from runweave.orchestration.models import RunStatus
from runweave.orchestration.registry import RunRegistry
from runweave.orchestration.scheduler import Scheduler
from runweave.tools.contracts import HealthQuery
from runweave.tools.fake import FakeHealthTool
from runweave.tools.retry import RetryingHealthTool, retry_after_seconds


@pytest.mark.parametrize(
    ("header", "expected"),
    [("2", 2), ("Fri, 06 Nov 2026 00:00:03 GMT", 3), ("invalid", 0), ("-1", 0)],
)
def test_retry_after_parsing(header: str, expected: int) -> None:
    assert retry_after_seconds(header, datetime(2026, 11, 6, tzinfo=UTC)) == expected


def retrying_tool(client: httpx.AsyncClient, *, delay: float = 0.001) -> RetryingHealthTool:
    return RetryingHealthTool(FakeHealthTool(client), attempts=3, base_delay=delay, max_delay=2)


@pytest.mark.asyncio
@pytest.mark.parametrize(("status", "attempts"), [(503, 2), (429, 2), (401, 1), (422, 1)])
async def test_only_transient_http_errors_are_retried(status: int, attempts: int) -> None:
    calls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(status, headers={"Retry-After": "0"})
        return httpx.Response(
            200, json={"service": "payments", "status": "healthy", "evidence_id": "mock:1"}
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="https://mock-tools.invalid"
    ) as client:
        tool = retrying_tool(client)
        if attempts == 1:
            with pytest.raises(httpx.HTTPStatusError):
                await tool.execute(HealthQuery(service="payments"))
        else:
            assert (await tool.execute(HealthQuery(service="payments"))).status == "healthy"
    assert calls == attempts


@pytest.mark.asyncio
async def test_read_timeout_retries_stop_at_attempt_limit() -> None:
    calls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("Mock read timeout", request=request)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="https://mock-tools.invalid"
    ) as client:
        with pytest.raises(httpx.ReadTimeout):
            await retrying_tool(client).execute(HealthQuery(service="payments"))
    assert calls == 3


@pytest.mark.asyncio
async def test_retry_after_larger_than_remaining_budget_prevents_retry() -> None:
    calls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(429, headers={"Retry-After": "1"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="https://mock-tools.invalid"
    ) as client:
        deadline = asyncio.get_running_loop().time() + 0.5
        with pytest.raises(httpx.HTTPStatusError):
            async with deadline_scope(deadline, STEP_DEADLINE):
                await retrying_tool(client).execute(HealthQuery(service="payments"))
    assert calls == 1


@pytest.mark.asyncio
async def test_cancellation_during_backoff_prevents_another_attempt() -> None:
    first_response = asyncio.Event()
    calls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        first_response.set()
        return httpx.Response(503, headers={"Retry-After": "1"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="https://mock-tools.invalid"
    ) as client:
        task = asyncio.create_task(retrying_tool(client).execute(HealthQuery(service="payments")))
        await asyncio.wait_for(first_response.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert calls == 1


@pytest.mark.asyncio
async def test_step_deadline_during_attempt_times_out_without_retry() -> None:
    calls = 0

    async def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        await asyncio.Event().wait()
        raise AssertionError("Unreachable")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="https://mock-tools.invalid"
    ) as client:
        runner = AgentRunner(FakeAgentModel(), retrying_tool(client), step_timeout=0.05)
        scheduler = Scheduler(
            runner, RunRegistry(10, 60), worker_count=1, queue_capacity=1, cleanup_interval=60
        )
        async with scheduler.running():
            run = scheduler.submit(IncidentRequest(service="payments", description="Latency"))
            await asyncio.wait_for(scheduler.join(), 2)
            snapshot = scheduler.get(run.id)
            assert snapshot is not None and snapshot.status == RunStatus.TIMED_OUT
            assert snapshot.error is not None and snapshot.error.code == STEP_DEADLINE
    assert calls == 1


@pytest.mark.asyncio
async def test_run_deadline_interrupts_retry_attempt_before_step_deadline() -> None:
    calls = 0

    async def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(503)
        await asyncio.Event().wait()
        raise AssertionError("Unreachable")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="https://mock-tools.invalid"
    ) as client:
        runner = AgentRunner(FakeAgentModel(), retrying_tool(client), step_timeout=2)
        scheduler = Scheduler(
            runner,
            RunRegistry(10, 60),
            worker_count=1,
            queue_capacity=1,
            cleanup_interval=60,
            run_timeout=0.2,
        )
        async with scheduler.running():
            run = scheduler.submit(IncidentRequest(service="payments", description="Latency"))
            await asyncio.wait_for(scheduler.join(), 2)
            snapshot = scheduler.get(run.id)
            assert snapshot is not None and snapshot.status == RunStatus.TIMED_OUT
            assert snapshot.error is not None and snapshot.error.code == "RUN_DEADLINE_EXCEEDED"
    assert calls == 2


@pytest.mark.asyncio
async def test_translated_cancellation_does_not_start_another_attempt() -> None:
    started = asyncio.Event()
    calls = 0

    async def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return httpx.Response(503)
        raise AssertionError("Unreachable")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="https://mock-tools.invalid"
    ) as client:
        task = asyncio.create_task(retrying_tool(client).execute(HealthQuery(service="payments")))
        await asyncio.wait_for(started.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert calls == 1
