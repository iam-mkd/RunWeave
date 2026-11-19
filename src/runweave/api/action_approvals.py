"""Decide agent-declared approvals (`ctx.request_approval`): the Approvals inbox and CLI.

A decision names the exact request (argument digest, policy version and expiry); the same
decision again is accepted, an opposing one conflicts. Without PostgreSQL there are none.
"""

from typing import Annotated, Any, Literal, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from runweave.action_approvals import ActionApproval, ApprovalChoice
from runweave.api.dependencies import audit, get_principal, get_scheduler, require_scope
from runweave.approvals import ApprovalError
from runweave.core.runtime import Runtime
from runweave.orchestration.backend import RunBackend
from runweave.orchestration.durable import DurableScheduler
from runweave.security import Principal
from runweave.storage.engine import storage_errors

router = APIRouter(prefix="/v1/approvals", tags=["approvals"])
PrincipalDependency = Annotated[Principal, Depends(get_principal)]
SchedulerDependency = Annotated[RunBackend, Depends(get_scheduler)]
NOT_FOUND = {"code": "APPROVAL_NOT_FOUND", "message": "No such approval request"}


def _checked(request: Request, approval: ActionApproval) -> ActionApproval:
    cast(Runtime, request.app.state.runtime).content_policy.check(approval.model_dump_json())
    return approval


@router.get("")
async def list_approvals(
    request: Request,
    principal: PrincipalDependency,
    scheduler: SchedulerDependency,
    status: Annotated[Literal["pending", "all"], Query()] = "pending",
) -> dict[str, Any]:
    require_scope(request, principal, "runs:read")
    if not isinstance(scheduler, DurableScheduler):
        return {"approvals": [], "durable": False}
    with storage_errors():
        approvals = await scheduler.action_approvals.list(principal, status=status)
    audit(request, "approvals:list", "allowed", principal)
    return {"approvals": [_checked(request, a) for a in approvals], "durable": True}


@router.get("/{approval_id}")
async def get_approval(
    approval_id: UUID,
    request: Request,
    principal: PrincipalDependency,
    scheduler: SchedulerDependency,
) -> ActionApproval:
    require_scope(request, principal, "runs:read")
    if not isinstance(scheduler, DurableScheduler):
        raise HTTPException(404, detail=NOT_FOUND)
    with storage_errors():
        approval = await scheduler.action_approvals.get(approval_id, principal)
    if approval is None:
        audit(request, "approvals:read", "owner", principal)
        raise HTTPException(404, detail=NOT_FOUND)
    return _checked(request, approval)


@router.post("/{approval_id}")
async def decide_approval(
    approval_id: UUID,
    choice: ApprovalChoice,
    request: Request,
    principal: PrincipalDependency,
    scheduler: SchedulerDependency,
) -> ActionApproval:
    require_scope(request, principal, "approvals:decide")
    require_scope(request, principal, "runs:read")
    if not isinstance(scheduler, DurableScheduler):
        raise HTTPException(404, detail=NOT_FOUND)
    try:
        with storage_errors():
            approval = await scheduler.action_approvals.decide(approval_id, principal, choice)
    except ApprovalError as exc:
        audit(request, "approvals:decide", "conflict", principal)
        raise HTTPException(409, detail={"code": exc.code, "message": "Decision refused"}) from None
    if approval is None:
        audit(request, "approvals:decide", "owner", principal)
        raise HTTPException(404, detail=NOT_FOUND)
    audit(request, "approvals:decide", choice.decision, principal)
    return _checked(request, approval)
