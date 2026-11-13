from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from runweave.agents.contracts import AgentExecutor
from runweave.api.health import router as health_router
from runweave.api.metrics import router as metrics_router
from runweave.api.runs import router as runs_router
from runweave.core.runtime import open_runtime
from runweave.core.settings import Settings


def create_app(
    settings: Settings | None = None,
    *,
    executor: AgentExecutor | None = None,
    mock_transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    config = settings if settings is not None else Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        try:
            async with open_runtime(
                config, executor=executor, mock_transport=mock_transport
            ) as runtime:
                app.state.runtime = runtime
                yield
        finally:
            app.state.runtime = None

    app = FastAPI(title=config.app_name, version="0.1.0", lifespan=lifespan)
    app.include_router(health_router)
    app.include_router(runs_router)
    app.include_router(metrics_router)
    return app
