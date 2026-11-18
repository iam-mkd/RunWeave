from dataclasses import asdict
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request

from runweave.api.dependencies import get_principal, get_scheduler, require_scope
from runweave.orchestration.backend import RunBackend
from runweave.security import Principal

router = APIRouter(prefix="/v1/agents", tags=["agents"])


@router.get("")
async def list_agents(
    scheduler: Annotated[RunBackend, Depends(get_scheduler)],
    principal: Annotated[Principal, Depends(get_principal)],
    request: Request,
) -> dict[str, list[dict[str, Any]]]:
    require_scope(request, principal, "runs:read")
    catalog = scheduler.agents
    return {
        "agents": [
            {**asdict(item.info), "default": item.info.name == catalog.default} for item in catalog
        ]
    }
