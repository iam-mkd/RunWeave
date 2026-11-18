from datetime import datetime
from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask

from runweave.agents.contracts import IncidentRequest
from runweave.api.dependencies import audit, get_principal, get_scheduler, require_scope
from runweave.api.events import EventStreams
from runweave.core.runtime import Runtime
from runweave.orchestration.admission import AdmissionRejected, InvalidRunInput, UnknownAgent
from runweave.orchestration.backend import RunBackend
from runweave.orchestration.models import (
    AgentName,
    AgentRunRequest,
    RunPage,
    RunSnapshot,
    RunStatus,
)
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
    submission: AgentRunRequest | IncidentRequest,
    response: Response,
    scheduler: SchedulerDependency,
    principal: PrincipalDependency,
    request: Request,
) -> RunSnapshot:
    require_scope(request, principal, "runs:submit")
    try:
        snapshot = await scheduler.submit(submission, principal)
    except UnknownAgent:
        raise HTTPException(
            status_code=422,
            detail={"code": "UNKNOWN_AGENT", "message": "No agent with that name is registered"},
        ) from None
    except InvalidRunInput as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "INVALID_INPUT",
                "message": "Input does not match the agent's input schema",
                "errors": exc.errors,
            },
        ) from None
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


@router.get("")
async def list_runs(
    scheduler: SchedulerDependency,
    principal: PrincipalDependency,
    request: Request,
    agent: Annotated[AgentName | None, Query()] = None,
    status: Annotated[RunStatus | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    before: Annotated[datetime | None, Query()] = None,
) -> RunPage:
    require_scope(request, principal, "runs:read")
    runs = await scheduler.list(principal, agent=agent, status=status, limit=limit, before=before)
    audit(request, "runs:list", "allowed", principal)
    return RunPage(runs=runs)


@router.get("/{run_id}")
async def get_run(
    run_id: UUID, scheduler: SchedulerDependency, principal: PrincipalDependency, request: Request
) -> RunSnapshot:
    require_scope(request, principal, "runs:read")
    snapshot = await scheduler.get(run_id, principal)
    audit(request, "runs:read", "allowed" if snapshot else "owner", principal)
    return require_run(snapshot, request)


@router.get("/{run_id}/events", response_class=StreamingResponse)
async def run_events(
    run_id: UUID, scheduler: SchedulerDependency, principal: PrincipalDependency, request: Request
) -> StreamingResponse:
    """Server-sent events: one `snapshot` event per change until the run is terminal."""
    require_scope(request, principal, "runs:read")
    require_run(await scheduler.get(run_id, principal), request)
    streams = cast(EventStreams, request.app.state.event_streams)
    lease = streams.acquire()
    if lease is None:
        raise HTTPException(
            status_code=503,
            detail={"code": "EVENT_STREAMS_BUSY", "message": "Too many open event streams"},
            headers={"Retry-After": "1"},
        )
    runtime = cast(Runtime, request.app.state.runtime)
    audit(request, "runs:events", "allowed", principal)
    return StreamingResponse(
        streams.frames(scheduler, run_id, principal, lease, runtime.content_policy.check),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        # Releases the slot even if the stream body never starts.
        background=BackgroundTask(lease.release),
    )


@router.post("/{run_id}/cancel")
async def cancel_run(
    run_id: UUID, scheduler: SchedulerDependency, principal: PrincipalDependency, request: Request
) -> RunSnapshot:
    require_scope(request, principal, "runs:cancel")
    require_scope(request, principal, "runs:read")
    snapshot = await scheduler.cancel(run_id, principal)
    audit(request, "runs:cancel", "allowed" if snapshot else "owner", principal)
    return require_run(snapshot, request)
