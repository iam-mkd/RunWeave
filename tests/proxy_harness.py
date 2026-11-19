"""Test harness: a RunWeave app with a live proxy binding, without running an agent."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import httpx

from runweave.app import create_app
from runweave.budgets import BudgetState, MemoryBudget
from runweave.core.settings import Settings
from runweave.steps import StepEvent


@dataclass
class Steps:
    events: dict[int, StepEvent] = field(default_factory=dict)

    async def begin(self, title: str, detail: str | None) -> StepEvent | None:
        event = StepEvent(
            index=len(self.events), title=title, detail=detail, status="running",
            started_at=datetime.now(UTC),
        )
        self.events[event.index] = event
        return event

    async def end(self, event: StepEvent) -> None:
        self.events[event.index] = event

    def details(self) -> list[tuple[str, str | None]]:
        return [(e.status, e.detail) for e in self.events.values()]


@dataclass
class Session:
    client: httpx.AsyncClient
    token: str
    ledger: MemoryBudget
    steps: Steps

    def openai(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    def anthropic(self) -> dict[str, str]:
        return {"x-api-key": self.token, "anthropic-version": "2023-06-01"}


@asynccontextmanager
async def proxy_session(
    *, upstream: httpx.AsyncBaseTransport | None = None, deadline: float = 30, **settings: Any
) -> AsyncIterator[Session]:
    config = Settings(proxy_public_url="http://test", **settings)
    app = create_app(config, agents=[], upstream_transport=upstream)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        ledger = MemoryBudget(BudgetState(policy=config.budget_policy()))
        steps = Steps()
        expires = asyncio.get_running_loop().time() + deadline
        with app.state.runtime.proxy.bound(
            run_id=uuid4(), attempt=1, agent="harness", ledger=ledger, steps=steps,
            expires_at=expires,
        ) as proxy:
            yield Session(client, proxy.token, ledger, steps)


CHAT = {"model": "gpt-test", "messages": [{"role": "user", "content": "hello"}]}
CHAT_TOOLS = {
    **CHAT,
    "tools": [{"type": "function", "function": {"name": "lookup_weather", "parameters": {"type": "object"}}}],
}
MESSAGES = {"model": "claude-test", "max_tokens": 100, "messages": [{"role": "user", "content": "hello"}]}
