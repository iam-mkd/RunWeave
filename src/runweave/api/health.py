from typing import Literal, cast

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel

from runweave.core.runtime import Runtime

router = APIRouter(prefix="/health", tags=["health"])


class HealthResponse(BaseModel):
    status: Literal["alive", "ready"]


@router.get("/live")
async def liveness() -> HealthResponse:
    return HealthResponse(status="alive")


@router.get("/ready")
async def readiness(request: Request) -> HealthResponse:
    runtime = cast(Runtime | None, getattr(request.app.state, "runtime", None))
    if runtime is None or runtime.client.is_closed:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Application is not ready"
        )
    return HealthResponse(status="ready")
