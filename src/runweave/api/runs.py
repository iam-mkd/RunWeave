from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from runweave.agents.contracts import IncidentRequest
from runweave.api.dependencies import audit, get_principal, get_scheduler, require_scope
from runweave.core.runtime import Runtime
from runweave.orchestration.backend import RunBackend
from runweave.orchestration.models import RunSnapshot
from runweave.orchestration.scheduler import AdmissionRejected
from runweave.security import PolicyDenied, Principal, TenantQuotaExceeded

router = APIRouter(prefix="/v1/runs", tags=["runs"])
SchedulerDependency = Annotated[RunBackend, Depends(get_scheduler)]
PrincipalDependency = Annotated[Principal, Depends(get_principal)]


def require_run(snapshot: RunSnapshot | None, request: Request) -> RunSnapshot:
    if snapshot is None:
        raise HTTPException(
            status_code=404, detail={"code": "RUN_NOT_FOUND", "message": "Run not found or expired"}
        )
    runtime = cast(Runtime, request.app.state.runtime)
    try:
        runtime.content_policy.check(snapshot.model_dump_json())
    except PolicyDenied:
        audit(request, "response", "content")
        raise
    return snapshot


@router.post("", status_code=202)
async def submit_run(
    incident: IncidentRequest,
    response: Response,
    scheduler: SchedulerDependency,
    principal: PrincipalDependency,
    request: Request,
) -> RunSnapshot:
    require_scope(request, principal, "runs:submit")
    try:
        snapshot = await scheduler.submit(incident, principal)
    except TenantQuotaExceeded:
        audit(request, "runs:submit", "quota", principal)
        raise HTTPException(
            status_code=429,
            detail={"code": "TENANT_QUOTA", "message": "Tenant admission limit reached"},
            headers={"Retry-After": "1"},
        ) from None
    except PolicyDenied:
        audit(request, "runs:submit", "content", principal)
        raise HTTPException(
            status_code=422,
            detail={"code": "CONTENT_REJECTED", "message": "Input rejected by execution policy"},
        ) from None
    except AdmissionRejected as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": exc.code, "message": "Run admission unavailable; retry later"},
            headers={"Retry-After": "1"},
        ) from exc
    response.headers["Location"] = snapshot.status_url
    audit(request, "runs:submit", "allowed", principal)
    return snapshot


@router.get("/{run_id}")
async def get_run(
    run_id: UUID, scheduler: SchedulerDependency, principal: PrincipalDependency, request: Request
) -> RunSnapshot:
    require_scope(request, principal, "runs:read")
    snapshot = await scheduler.get(run_id, principal)
    audit(request, "runs:read", "allowed" if snapshot else "owner", principal)
    return require_run(snapshot, request)


@router.post("/{run_id}/cancel")
async def cancel_run(
    run_id: UUID, scheduler: SchedulerDependency, principal: PrincipalDependency, request: Request
) -> RunSnapshot:
    require_scope(request, principal, "runs:cancel")
    require_scope(request, principal, "runs:read")
    snapshot = await scheduler.cancel(run_id, principal)
    audit(request, "runs:cancel", "allowed" if snapshot else "owner", principal)
    return require_run(snapshot, request)
