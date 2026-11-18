from dataclasses import asdict
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from runweave.agents.catalog import AgentCatalog, AgentLoadError, RegisteredAgent
from runweave.agents.http import AgentUrlNotAllowed, HttpAgentConnector, ManifestRejected
from runweave.api.dependencies import audit, get_principal, get_scheduler, require_scope
from runweave.core.runtime import Runtime
from runweave.orchestration.backend import RunBackend
from runweave.security import Principal

router = APIRouter(prefix="/v1/agents", tags=["agents"])


class HttpAgentTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str = Field(min_length=1, max_length=2048, examples=["http://127.0.0.1:9100"])


def _describe(item: RegisteredAgent, catalog: AgentCatalog) -> dict[str, Any]:
    return {**asdict(item.info), "default": item.info.name == catalog.default}


def _http(request: Request) -> tuple[HttpAgentConnector, AgentCatalog, int]:
    runtime = cast(Runtime | None, getattr(request.app.state, "runtime", None))
    if runtime is None or runtime.connector is None or runtime.agents is None:
        raise HTTPException(
            status_code=503,
            detail={"code": "SCHEDULER_UNAVAILABLE", "message": "Application is not ready"},
            headers={"Retry-After": "1"},
        )
    limit = int(request.app.state.http_agent_max_registered)
    return runtime.connector, runtime.agents, limit


async def _discover(connector: HttpAgentConnector, target: HttpAgentTarget) -> Any:
    try:
        return await connector.connect(target.url, removable=True)
    except AgentUrlNotAllowed as error:
        raise HTTPException(
            status_code=422, detail={"code": error.code, "message": str(error)}
        ) from None
    except ManifestRejected as error:
        raise HTTPException(
            status_code=502, detail={"code": error.code, "message": str(error)}
        ) from None


@router.get("")
async def list_agents(
    scheduler: Annotated[RunBackend, Depends(get_scheduler)],
    principal: Annotated[Principal, Depends(get_principal)],
    request: Request,
) -> dict[str, list[dict[str, Any]]]:
    require_scope(request, principal, "runs:read")
    catalog = scheduler.agents
    return {"agents": [_describe(item, catalog) for item in catalog]}


@router.post("/http/check")
async def check_http_agent(
    target: HttpAgentTarget,
    principal: Annotated[Principal, Depends(get_principal)],
    request: Request,
) -> dict[str, Any]:
    """Fetch an HTTP agent's manifest without registering it."""
    require_scope(request, principal, "agents:manage")
    connector, catalog, _ = _http(request)
    found = await _discover(connector, target)
    base = found[0].info.target
    conflicts = catalog.conflicts(base, [item.info.name for item in found])
    return {
        "url": base,
        "agents": [
            {
                "name": item.info.name,
                "version": item.info.version,
                "description": item.info.description,
                "max_steps": item.info.max_steps,
                "conflict": item.info.name in conflicts,
            }
            for item in found
        ],
    }


@router.post("/http", status_code=201)
async def register_http_agent(
    target: HttpAgentTarget,
    principal: Annotated[Principal, Depends(get_principal)],
    request: Request,
) -> dict[str, Any]:
    """Register (or refresh) the agents served at a URL until the server stops."""
    require_scope(request, principal, "agents:manage")
    connector, catalog, limit = _http(request)
    found = await _discover(connector, target)
    try:
        catalog.replace_source(found[0].info.target, found, limit=limit)
    except AgentLoadError as error:
        raise HTTPException(
            status_code=409, detail={"code": error.code, "message": str(error)}
        ) from None
    audit(request, "agents:manage", "registered", principal)
    return {"agents": [_describe(item, catalog) for item in found]}


@router.delete("/{name}", status_code=204)
async def remove_agent(
    name: str,
    principal: Annotated[Principal, Depends(get_principal)],
    request: Request,
) -> Response:
    require_scope(request, principal, "agents:manage")
    _, catalog, _ = _http(request)
    try:
        removed = catalog.remove(name)
    except AgentLoadError as error:
        raise HTTPException(
            status_code=409, detail={"code": error.code, "message": str(error)}
        ) from None
    if removed is None:
        raise HTTPException(
            status_code=404, detail={"code": "UNKNOWN_AGENT", "message": "Agent not found"}
        )
    audit(request, "agents:manage", "removed", principal)
    return Response(status_code=204)
