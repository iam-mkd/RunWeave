from typing import Annotated

from fastapi import APIRouter, Depends, Response

from runweave.api.dependencies import get_scheduler
from runweave.orchestration.backend import RunBackend

router = APIRouter()


@router.get("/metrics", include_in_schema=False)
async def metrics(scheduler: Annotated[RunBackend, Depends(get_scheduler)]) -> Response:
    return Response(await scheduler.metrics(), media_type="text/plain; version=0.0.4")
