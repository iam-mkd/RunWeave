from typing import cast

from fastapi import HTTPException, Request

from runweave.core.runtime import Runtime
from runweave.orchestration.scheduler import Scheduler


async def get_scheduler(request: Request) -> Scheduler:
    runtime = cast(Runtime | None, getattr(request.app.state, "runtime", None))
    if runtime is None:
        raise HTTPException(
            status_code=503,
            detail={"code": "SCHEDULER_UNAVAILABLE", "message": "Application is not ready"},
            headers={"Retry-After": "1"},
        )
    return runtime.scheduler
