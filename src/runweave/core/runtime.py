from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import httpx

from runweave.agents.contracts import AgentExecutor
from runweave.agents.fake import FakeAgentModel
from runweave.agents.runner import AgentRunner
from runweave.core.settings import Settings
from runweave.orchestration.registry import RunRegistry
from runweave.orchestration.scheduler import Scheduler
from runweave.tools.fake import FakeHealthTool, fake_health_response
from runweave.tools.retry import RetryingHealthTool


@dataclass(frozen=True)
class Runtime:
    client: httpx.AsyncClient
    runner: AgentRunner
    scheduler: Scheduler


@asynccontextmanager
async def open_runtime(
    settings: Settings, *, executor: AgentExecutor | None = None
) -> AsyncIterator[Runtime]:
    async with httpx.AsyncClient(
        base_url="https://mock-tools.invalid",
        transport=httpx.MockTransport(fake_health_response),
        timeout=httpx.Timeout(settings.http_timeout_seconds),
        limits=httpx.Limits(
            max_connections=settings.http_max_connections,
            max_keepalive_connections=settings.http_max_connections,
        ),
        follow_redirects=False,
        trust_env=False,
    ) as client:
        health_tool = RetryingHealthTool(
            FakeHealthTool(client),
            attempts=settings.tool_retry_attempts,
            base_delay=settings.tool_retry_base_delay_seconds,
            max_delay=settings.tool_retry_max_delay_seconds,
        )
        runner = AgentRunner(
            FakeAgentModel(), health_tool, step_timeout=settings.step_timeout_seconds
        )
        scheduler = Scheduler(
            executor if executor is not None else runner,
            RunRegistry(settings.history_max_records, settings.history_ttl_seconds),
            worker_count=settings.worker_count,
            queue_capacity=settings.queue_capacity,
            cleanup_interval=settings.history_cleanup_interval_seconds,
            run_timeout=settings.run_timeout_seconds,
            shutdown_grace=settings.shutdown_grace_seconds,
        )
        async with scheduler.running():
            yield Runtime(client=client, runner=runner, scheduler=scheduler)
