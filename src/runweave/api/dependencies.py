from typing import cast

from fastapi import HTTPException, Request

from runweave.core.runtime import Runtime
from runweave.identity import AuthenticationFailed, IdentityProvider
from runweave.orchestration.backend import RunBackend
from runweave.security import PolicyDenied, Principal


def audit(request: Request, action: str, reason: str, principal: Principal | None = None) -> None:
    runtime = cast(Runtime | None, getattr(request.app.state, "runtime", None))
    if runtime is not None:
        runtime.telemetry.audit(action, reason, principal)


async def get_principal(request: Request) -> Principal:
    provider = cast(IdentityProvider, request.app.state.identity_provider)
    try:
        return provider.authenticate(request.headers.get("Authorization"))
    except AuthenticationFailed:
        audit(request, "authenticate", "credentials")
        raise HTTPException(
            status_code=401,
            detail={"code": "UNAUTHENTICATED", "message": "Valid credentials required"},
            headers={"WWW-Authenticate": "Bearer"},
        ) from None


def require_scope(request: Request, principal: Principal, scope: str) -> None:
    try:
        principal.require(scope)
    except PolicyDenied:
        audit(request, scope, "scope", principal)
        raise HTTPException(
            status_code=403,
            detail={"code": "FORBIDDEN", "message": "Operation not permitted"},
        ) from None


async def get_scheduler(request: Request) -> RunBackend:
    runtime = cast(Runtime | None, getattr(request.app.state, "runtime", None))
    if runtime is None:
        raise HTTPException(
            status_code=503,
            detail={"code": "SCHEDULER_UNAVAILABLE", "message": "Application is not ready"},
            headers={"Retry-After": "1"},
        )
    return runtime.scheduler
