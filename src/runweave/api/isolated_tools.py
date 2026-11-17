from typing import Annotated, cast

from fastapi import APIRouter, Depends, HTTPException, Request

from runweave.api.dependencies import audit, get_principal, require_scope
from runweave.core.runtime import Runtime
from runweave.security import Principal
from runweave.tools.container_runtime import IsolationError
from runweave.tools.isolated import BrowserRead, IsolatedResult, IsolatedTools, PythonProgram

router = APIRouter(prefix="/v1/tools", tags=["isolated tools"])
PrincipalDependency = Annotated[Principal, Depends(get_principal)]


def tools(request: Request) -> IsolatedTools:
    runtime = cast(Runtime | None, getattr(request.app.state, "runtime", None))
    if runtime is None or runtime.isolated is None or not runtime.isolated.ready:
        raise HTTPException(503, detail={"code": "ISOLATED_TOOLS_UNAVAILABLE"})
    return runtime.isolated


@router.post("/python")
async def python_tool(
    program: PythonProgram, request: Request, principal: PrincipalDependency
) -> IsolatedResult:
    require_scope(request, principal, "tools:python_sandbox")
    try:
        result = await tools(request).python(program, principal)
    except IsolationError as exc:
        audit(request, "python_sandbox", "rejected", principal)
        raise HTTPException(
            503 if exc.code.endswith(("UNAVAILABLE", "BUSY", "UNCONFIRMED")) else 422,
            detail={"code": exc.code},
        ) from None
    audit(request, "python_sandbox", "allowed", principal)
    return result


@router.post("/browser")
async def browser_tool(
    read: BrowserRead, request: Request, principal: PrincipalDependency
) -> IsolatedResult:
    require_scope(request, principal, "tools:browser_read")
    try:
        result = await tools(request).browser(read, principal)
    except IsolationError as exc:
        audit(request, "browser_read", "rejected", principal)
        raise HTTPException(
            503 if exc.code.endswith(("UNAVAILABLE", "BUSY", "UNCONFIRMED")) else 422,
            detail={"code": exc.code},
        ) from None
    audit(request, "browser_read", "allowed", principal)
    return result
