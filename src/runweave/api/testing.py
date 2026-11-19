"""Testing jobs and saved reports for the Load tests, Evals and Shadow screens.

The workspace starts the same commands as the CLI (see `runweave.jobs`) and reads the same
report files. Nothing here returns local paths.
"""

import asyncio
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from runweave.api.dependencies import audit, get_principal, require_scope
from runweave.jobs import (
    Job,
    JobManager,
    JobRequest,
    JobRunning,
    Kind,
    NoProject,
    describe,
    list_reports,
    read_report,
)
from runweave.project import Project, ProjectError
from runweave.security import Principal

router = APIRouter(tags=["testing"])
RUN_SCOPE = "tests:run"
READ_SCOPE = "tests:read"
PrincipalDependency = Annotated[Principal, Depends(get_principal)]
NO_PROJECT = {
    "code": "NO_PROJECT",
    "message": "This workspace has no runweave.toml. Create a project with `runweave init`, "
    "then start the workspace from its folder with `runweave serve` (or pass --project).",
}


def _jobs(request: Request) -> JobManager:
    return cast(JobManager, request.app.state.jobs)


def _project(jobs: JobManager) -> Project:
    try:
        return jobs.project()
    except NoProject:
        raise HTTPException(status_code=404, detail=NO_PROJECT) from None
    except ProjectError as error:
        raise HTTPException(
            status_code=409, detail={"code": "PROJECT_INVALID", "message": str(error)}
        ) from None


def _job(jobs: JobManager, job_id: str) -> Job:
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(
            status_code=404, detail={"code": "JOB_NOT_FOUND", "message": "No such job"}
        )
    return job


@router.get("/v1/jobs/project")
async def project_info(request: Request, principal: PrincipalDependency) -> dict[str, Any]:
    require_scope(request, principal, READ_SCOPE)
    project = _project(_jobs(request))
    return await asyncio.to_thread(describe, project)


@router.post("/v1/jobs", status_code=202)
async def start_job(
    body: JobRequest, request: Request, principal: PrincipalDependency
) -> dict[str, Any]:
    require_scope(request, principal, RUN_SCOPE)
    jobs = _jobs(request)
    _project(jobs)
    try:
        job = jobs.start(body)
    except JobRunning:
        raise HTTPException(
            status_code=409,
            detail={"code": "JOB_RUNNING", "message": "Another test is running; wait or cancel it"},
        ) from None
    audit(request, RUN_SCOPE, "started", principal)
    return job.view()


@router.get("/v1/jobs")
async def recent_jobs(request: Request, principal: PrincipalDependency) -> dict[str, Any]:
    require_scope(request, principal, READ_SCOPE)
    return {"jobs": [job.view() for job in _jobs(request).recent()]}


@router.get("/v1/jobs/{job_id}")
async def job_events(
    job_id: str,
    request: Request,
    principal: PrincipalDependency,
    after: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    require_scope(request, principal, READ_SCOPE)
    job = _job(_jobs(request), job_id)
    return {"job": job.view(), "events": job.after(after)}


@router.post("/v1/jobs/{job_id}/cancel")
async def cancel_job(
    job_id: str, request: Request, principal: PrincipalDependency
) -> dict[str, Any]:
    require_scope(request, principal, RUN_SCOPE)
    jobs = _jobs(request)
    job = _job(jobs, job_id)
    await jobs.cancel(job)
    audit(request, RUN_SCOPE, "cancelled", principal)
    return job.view()


@router.get("/v1/reports")
async def reports(
    request: Request, principal: PrincipalDependency, kind: Kind | None = None
) -> dict[str, Any]:
    require_scope(request, principal, READ_SCOPE)
    project = _project(_jobs(request))
    return {"reports": await asyncio.to_thread(list_reports, project.eval.reports, kind)}


@router.get("/v1/reports/{report_id}")
async def report(
    report_id: str, request: Request, principal: PrincipalDependency
) -> dict[str, Any]:
    require_scope(request, principal, READ_SCOPE)
    project = _project(_jobs(request))
    found = await asyncio.to_thread(read_report, project.eval.reports, report_id)
    if found is None:
        raise HTTPException(
            status_code=404, detail={"code": "REPORT_NOT_FOUND", "message": "No such report"}
        )
    return found
