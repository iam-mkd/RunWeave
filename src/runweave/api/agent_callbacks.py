"""Callbacks from HTTP agents to RunWeave: `ctx.request_approval` for agents in their own
process.

The agent posts `{action, arguments, expires_in, wait_seconds}` to the `approvals_url` it was
sent, with its run attempt's proxy token as a bearer token. The request is recorded (or the
existing one found: repeating it is safe) and the call waits up to `wait_seconds` for a
decision; while it is pending the agent repeats the call. Responses use the agent
contract's error shape (`{"error": {"code": ...}}`) and never echo the arguments.
"""

from typing import Annotated, Any, cast

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from runweave.action_approvals import ACTION_PATTERN, ApprovalRefused, canonical
from runweave.approvals import ApprovalError
from runweave.core.runtime import Runtime
from runweave.deadline import DeadlineExceeded
from runweave.proxy.bindings import APPROVALS_PATH
from runweave.security import PolicyDenied
from runweave.storage.engine import StorageUnavailable, storage_errors

router = APIRouter(tags=["agent callbacks"], include_in_schema=False)
MAX_WAIT_SECONDS = 25
MAX_BODY_BYTES = 70_000


class ApprovalCall(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    action: Annotated[str, Field(pattern=ACTION_PATTERN.pattern)]
    arguments: Any
    expires_in: float | None = Field(default=None, gt=0, le=86400)
    wait_seconds: float = Field(default=20, ge=0, le=MAX_WAIT_SECONDS)


def _error(status: int, code: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code}}, status_code=status)


@router.post(APPROVALS_PATH)
async def request_approval(request: Request) -> JSONResponse:
    runtime = cast(Runtime | None, getattr(request.app.state, "runtime", None))
    header = request.headers.get("authorization", "")
    token = header[7:] if header.lower().startswith("bearer ") else None
    binding = None if runtime is None or runtime.proxy is None else runtime.proxy.resolve(token)
    if binding is None:
        return _error(401, "APPROVAL_TOKEN_INVALID")
    if binding.approvals is None:
        return _error(404, "DURABLE_APPROVAL_REQUIRED")
    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        return _error(413, "APPROVAL_REQUEST_TOO_LARGE")
    assert runtime is not None
    try:
        call = ApprovalCall.model_validate_json(raw)
        runtime.content_policy.check(canonical(call.arguments))
    except PolicyDenied:  # a ValueError too, so it is checked first
        return _error(422, "CONTENT_REJECTED")
    except (ValidationError, ValueError):
        return _error(422, "APPROVAL_REQUEST_INVALID")
    try:
        with storage_errors():
            row = await binding.approvals.submit(
                call.action, call.arguments, call.expires_in, wait=call.wait_seconds
            )
    except (ApprovalRefused, ApprovalError) as error:
        return _error(409, error.code)
    except DeadlineExceeded:
        return _error(409, "AGENT_DEADLINE_EXCEEDED")
    except StorageUnavailable:
        return _error(503, "STORAGE_UNAVAILABLE")
    return JSONResponse(
        {
            "approval": {
                "id": str(row["id"]),
                "status": row["status"],
                "decided_by": row["decided_by"],
                "decided_at": row["decided_at"].isoformat() if row["decided_at"] else None,
                "expires_at": row["expires_at"].isoformat(),
            }
        }
    )
