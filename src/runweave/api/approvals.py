from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request

from runweave.api.dependencies import audit, get_principal, get_scheduler, require_scope
from runweave.approvals import ApprovalDecision, ApprovalError, ApprovalSnapshot
from runweave.core.runtime import Runtime
from runweave.orchestration.backend import RunBackend
from runweave.orchestration.durable import DurableScheduler
from runweave.security import Principal
from runweave.storage.engine import storage_errors

router = APIRouter(prefix="/v1/runs", tags=["approvals"])


@router.get("/{run_id}/approval")
async def get_approval(
    run_id: UUID,
    request: Request,
    principal: Annotated[Principal, Depends(get_principal)],
    scheduler: Annotated[RunBackend, Depends(get_scheduler)],
) -> ApprovalSnapshot:
    require_scope(request, principal, "runs:read")
    if not isinstance(scheduler, DurableScheduler):
        raise HTTPException(404, detail={"code": "APPROVAL_NOT_FOUND"})
    with storage_errors():
        result = await scheduler.approvals.get(run_id, principal)
    if result is None:
        audit(request, "approval:read", "owner", principal)
        raise HTTPException(404, detail={"code": "APPROVAL_NOT_FOUND"})
    cast(Runtime, request.app.state.runtime).content_policy.check(result.model_dump_json())
    audit(request, "approval:read", "allowed", principal)
    return result


@router.post("/{run_id}/approval")
async def decide_approval(
    run_id: UUID,
    decision: ApprovalDecision,
    request: Request,
    principal: Annotated[Principal, Depends(get_principal)],
    scheduler: Annotated[RunBackend, Depends(get_scheduler)],
) -> ApprovalSnapshot:
    require_scope(request, principal, "approvals:decide")
    require_scope(request, principal, "runs:read")
    if not isinstance(scheduler, DurableScheduler):
        raise HTTPException(404, detail={"code": "APPROVAL_NOT_FOUND"})
    try:
        with storage_errors():
            result = await scheduler.approvals.decide(run_id, principal, decision)
    except ApprovalError as exc:
        audit(request, "approval:decide", "conflict", principal)
        raise HTTPException(409, detail={"code": exc.code}) from None
    if result is None:
        audit(request, "approval:decide", "owner", principal)
        raise HTTPException(404, detail={"code": "APPROVAL_NOT_FOUND"})
    cast(Runtime, request.app.state.runtime).content_policy.check(result.model_dump_json())
    audit(request, "approval:decide", decision.decision, principal)
    return result
