from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import httpx

from runweave.agents.contracts import AgentExecutor, AgentModel
from runweave.agents.fake import FakeAgentModel
from runweave.agents.groq import GroqAgentModel
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
    mock_transport = httpx.MockTransport(fake_health_response)
    groq_enabled = settings.model_provider == "groq"
    async with httpx.AsyncClient(
        base_url="https://mock-tools.invalid",
        transport=None if groq_enabled else mock_transport,
        mounts={"https://mock-tools.invalid": mock_transport} if groq_enabled else None,
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
        model: AgentModel = FakeAgentModel()
        if groq_enabled:
            model = GroqAgentModel(
                client,
                settings.groq_api_key,
                model=settings.groq_model,
                concurrency=settings.groq_concurrency,
                requests_per_minute=settings.groq_requests_per_minute,
            )
        runner = AgentRunner(
            model,
            health_tool,
            step_timeout=settings.step_timeout_seconds,
            max_steps=settings.agent_max_steps,
            completion_budget=settings.agent_completion_tokens,
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
