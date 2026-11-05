from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from runweave.api.health import router as health_router
from runweave.core.runtime import open_runtime
from runweave.core.settings import Settings


def create_app(settings: Settings | None = None) -> FastAPI:
    config = settings if settings is not None else Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with open_runtime(config) as runtime:
            app.state.runtime = runtime
            try:
                yield
            finally:
                app.state.runtime = None

    app = FastAPI(title=config.app_name, version="0.1.0", lifespan=lifespan)
    app.include_router(health_router)
    return app
