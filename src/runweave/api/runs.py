from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response

from runweave.agents.contracts import IncidentRequest
from runweave.api.dependencies import get_scheduler
from runweave.orchestration.backend import RunBackend
from runweave.orchestration.models import RunSnapshot
from runweave.orchestration.scheduler import AdmissionRejected

router = APIRouter(prefix="/v1/runs", tags=["runs"])
SchedulerDependency = Annotated[RunBackend, Depends(get_scheduler)]


def require_run(snapshot: RunSnapshot | None) -> RunSnapshot:
    if snapshot is None:
        raise HTTPException(
            status_code=404, detail={"code": "RUN_NOT_FOUND", "message": "Run not found or expired"}
        )
    return snapshot


@router.post("", status_code=202)
async def submit_run(
    incident: IncidentRequest, response: Response, scheduler: SchedulerDependency
) -> RunSnapshot:
    try:
        snapshot = await scheduler.submit(incident)
    except AdmissionRejected as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": exc.code, "message": "Run admission unavailable; retry later"},
            headers={"Retry-After": "1"},
        ) from exc
    response.headers["Location"] = snapshot.status_url
    return snapshot


@router.get("/{run_id}")
async def get_run(run_id: UUID, scheduler: SchedulerDependency) -> RunSnapshot:
    return require_run(await scheduler.get(run_id))


@router.post("/{run_id}/cancel")
async def cancel_run(run_id: UUID, scheduler: SchedulerDependency) -> RunSnapshot:
    return require_run(await scheduler.cancel(run_id))
