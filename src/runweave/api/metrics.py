from typing import Annotated

from fastapi import APIRouter, Depends, Response

from runweave.api.dependencies import get_scheduler
from runweave.orchestration.scheduler import Scheduler

router = APIRouter()


@router.get("/metrics", include_in_schema=False)
async def metrics(scheduler: Annotated[Scheduler, Depends(get_scheduler)]) -> Response:
    return Response(scheduler.metrics(), media_type="text/plain; version=0.0.4")
