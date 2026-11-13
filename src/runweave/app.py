from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from runweave.agents.contracts import AgentExecutor
from runweave.api.health import router as health_router
from runweave.api.metrics import router as metrics_router
from runweave.api.runs import router as runs_router
from runweave.core.runtime import open_runtime
from runweave.core.settings import Settings
from runweave.storage.engine import StorageUnavailable


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

    @app.exception_handler(StorageUnavailable)
    async def storage_unavailable(request: Request, exc: StorageUnavailable) -> JSONResponse:
        return JSONResponse(
            status_code=503,
            headers={"Retry-After": "1"},
            content={"detail": {"code": "STORAGE_UNAVAILABLE", "message": "Storage unavailable"}},
        )

    app.include_router(health_router)
    app.include_router(runs_router)
    app.include_router(metrics_router)
    return app
