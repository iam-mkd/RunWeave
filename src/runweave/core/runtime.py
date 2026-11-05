from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import httpx

from runweave.agents.fake import FakeAgentModel
from runweave.agents.runner import AgentRunner
from runweave.core.settings import Settings
from runweave.tools.fake import FakeHealthTool, fake_health_response


@dataclass(frozen=True)
class Runtime:
    client: httpx.AsyncClient
    runner: AgentRunner


@asynccontextmanager
async def open_runtime(settings: Settings) -> AsyncIterator[Runtime]:
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
        yield Runtime(client=client, runner=AgentRunner(FakeAgentModel(), FakeHealthTool(client)))
