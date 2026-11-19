from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from runweave.agents.catalog import RegisteredAgent, load_python_agents
from runweave.agents.contracts import AgentExecutor
from runweave.api.agents import router as agents_router
from runweave.api.approvals import router as approvals_router
from runweave.api.body_limit import RequestBodyLimit
from runweave.api.events import EventStreams
from runweave.api.health import router as health_router
from runweave.api.isolated_tools import router as isolated_router
from runweave.api.memory import router as memory_router
from runweave.api.metrics import router as metrics_router
from runweave.api.runs import router as runs_router
from runweave.core.runtime import open_runtime
from runweave.core.settings import Settings
from runweave.identity import IdentityProvider
from runweave.proxy.faults import CUSTOM_PROFILE, FaultPlan, FaultProfile, load_profiles
from runweave.proxy.mock import MockModel, load_fixtures
from runweave.proxy.router import ProxyConfig, install_log_filter
from runweave.proxy.router import router as proxy_router
from runweave.security import PolicyDenied
from runweave.storage.engine import StorageUnavailable


def _fault_profile(config: Settings) -> FaultProfile | None:
    name = config.proxy_fault_profile
    if not name:
        return None
    if name == CUSTOM_PROFILE:
        if not config.proxy_fault_inline:
            raise ValueError("The custom fault profile needs RUNWEAVE_PROXY_FAULT_INLINE")
        return FaultProfile(name=CUSTOM_PROFILE, faults=config.proxy_fault_inline)
    profiles = load_profiles(config.proxy_fault_profiles_file)
    if name not in profiles:
        raise ValueError(f"Unknown fault profile {name!r}; known: {', '.join(sorted(profiles))}")
    return profiles[name]


def create_app(
    settings: Settings | None = None,
    *,
    executor: AgentExecutor | None = None,
    mock_transport: httpx.AsyncBaseTransport | None = None,
    agents: Sequence[RegisteredAgent] | None = None,
    http_agents: Sequence[str] | None = None,
    upstream_transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    config = settings if settings is not None else Settings()
    # Imports configured agent modules once, before serving; requests never load code.
    registered = list(agents) if agents is not None else load_python_agents(config.agents)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        try:
            async with open_runtime(
                config,
                executor=executor,
                mock_transport=mock_transport,
                agents=registered,
                http_agents=http_agents,
                upstream_transport=upstream_transport,
            ) as runtime:
                app.state.runtime = runtime
                yield
        finally:
            app.state.runtime = None

    app = FastAPI(title=config.app_name, version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        RequestBodyLimit,
        runs_max_bytes=config.run_request_max_bytes,
        proxy_max_bytes=config.proxy_request_max_bytes,
    )
    app.state.proxy_config = ProxyConfig(
        mode=config.proxy_mode,
        mock=MockModel(load_fixtures(config.proxy_mock_fixtures)),
        faults=FaultPlan(_fault_profile(config), config.proxy_fault_seed),
        default_max_tokens=config.proxy_default_max_tokens,
        models=frozenset(config.proxy_models),
    )
    install_log_filter()
    app.state.identity_provider = IdentityProvider(config)
    app.state.http_agent_max_registered = config.http_agent_max_registered
    app.state.event_streams = EventStreams(
        max_subscribers=config.event_stream_max_subscribers,
        max_seconds=config.event_stream_max_seconds,
        heartbeat=config.event_stream_heartbeat_seconds,
    )

    @app.exception_handler(PolicyDenied)
    async def policy_denied(request: Request, exc: PolicyDenied) -> JSONResponse:
        return JSONResponse(
            status_code=403,
            content={"detail": {"code": "FORBIDDEN", "message": "Operation not permitted"}},
        )

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={"detail": {"code": "INVALID_REQUEST", "message": "Invalid request data"}},
        )

    @app.exception_handler(StorageUnavailable)
    async def storage_unavailable(request: Request, exc: StorageUnavailable) -> JSONResponse:
        return JSONResponse(
            status_code=503,
            headers={"Retry-After": "1"},
            content={"detail": {"code": "STORAGE_UNAVAILABLE", "message": "Storage unavailable"}},
        )

    app.include_router(health_router)
    app.include_router(runs_router)
    app.include_router(agents_router)
    app.include_router(proxy_router)
    app.include_router(approvals_router)
    app.include_router(memory_router)
    app.include_router(metrics_router)
    app.include_router(isolated_router)
    return app
