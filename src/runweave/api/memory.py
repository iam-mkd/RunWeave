from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from runweave.api.dependencies import audit, get_principal, get_scheduler, require_scope
from runweave.memory import MemoryFact, MemoryRejected, MemorySelection, MemoryWrite
from runweave.orchestration.backend import RunBackend
from runweave.orchestration.durable import DurableScheduler
from runweave.security import IdentityName, PolicyDenied, Principal
from runweave.storage.engine import storage_errors
from runweave.tools.contracts import ServiceName

router = APIRouter(prefix="/v1/memory", tags=["memory"])
PrincipalDependency = Annotated[Principal, Depends(get_principal)]
SchedulerDependency = Annotated[RunBackend, Depends(get_scheduler)]


def durable(scheduler: RunBackend) -> DurableScheduler:
    if not isinstance(scheduler, DurableScheduler):
        raise HTTPException(503, detail={"code": "MEMORY_REQUIRES_POSTGRES"})
    return scheduler


@router.get("")
async def recall_memory(
    service: ServiceName,
    request: Request,
    principal: PrincipalDependency,
    scheduler: SchedulerDependency,
    project: IdentityName | None = None,
) -> tuple[MemoryFact, ...]:
    require_scope(request, principal, "memory:read")
    try:
        with storage_errors():
            facts = await durable(scheduler).memory.recall(
                service, MemorySelection(project=project), principal
            )
    except PolicyDenied:
        audit(request, "memory:read", "policy", principal)
        raise
    except MemoryRejected as exc:
        audit(request, "memory:read", "invalid", principal)
        raise HTTPException(409, detail={"code": exc.code}) from None
    audit(request, "memory:read", "allowed", principal)
    return facts


@router.post("", status_code=201)
async def write_memory(
    entry: MemoryWrite,
    request: Request,
    principal: PrincipalDependency,
    scheduler: SchedulerDependency,
) -> MemoryFact:
    require_scope(request, principal, "memory:write")
    require_scope(request, principal, "runs:read")
    try:
        with storage_errors():
            fact = await durable(scheduler).memory.write(entry, principal)
    except PolicyDenied:
        audit(request, "memory:write", "policy", principal)
        raise
    except MemoryRejected as exc:
        audit(request, "memory:write", "rejected", principal)
        raise HTTPException(409, detail={"code": exc.code}) from None
    audit(request, "memory:write", "allowed", principal)
    return fact


@router.delete("/{fact_id}", status_code=204)
async def delete_memory(
    fact_id: UUID,
    request: Request,
    principal: PrincipalDependency,
    scheduler: SchedulerDependency,
) -> Response:
    require_scope(request, principal, "memory:delete")
    try:
        with storage_errors():
            deleted = await durable(scheduler).memory.delete(fact_id, principal)
    except PolicyDenied:
        audit(request, "memory:delete", "policy", principal)
        raise
    audit(request, "memory:delete", "allowed" if deleted else "owner", principal)
    if not deleted:
        raise HTTPException(404, detail={"code": "MEMORY_NOT_FOUND"})
    return Response(status_code=204)
