"""Run a project's agent through RunWeave's own HTTP API, in-process or on a running server.

In-process, RunWeave starts inside the command and listens on a free loopback port, so agents
reach its LLM proxy over a real socket (Python agents are imported from the project folder;
an HTTP agent's `command` is started and stopped around the run). With `--server`, cases are
submitted to that instance. Both paths use the same API calls, so the CLI and the workspace
run identical jobs.
"""

import asyncio
import os
import platform
import socket
import sys
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from importlib.metadata import PackageNotFoundError, version
from typing import Any

import httpx

from runweave.evals.assertions import CaseResult
from runweave.project import AgentSection, CandidateSection, Project, ProjectError

TERMINAL = {"SUCCEEDED", "FAILED", "TIMED_OUT", "CANCELLED"}
READY_PREFIX = "RUNWEAVE_AGENT_READY "


@dataclass(frozen=True)
class Target:
    client: httpx.AsyncClient
    mode: str
    agent: dict[str, Any]


def environment(mode: str) -> dict[str, Any]:
    try:
        runweave = version("runweave")
    except PackageNotFoundError:
        runweave = "source"
    return {
        "mode": mode,
        "runweave": runweave,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "cpu_count": os.cpu_count(),
    }


async def _start_agent(command: tuple[str, ...], project: Project) -> tuple[Any, str]:
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=project.root,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError as error:
        raise ProjectError(
            f"Cannot start the agent ({' '.join(command)}): {error.strerror}"
        ) from None
    assert process.stdout is not None
    try:
        async with asyncio.timeout(30):
            while True:
                line = (await process.stdout.readline()).decode(errors="replace")
                if not line:
                    raise ProjectError(
                        f"The agent command exited before it was ready: {' '.join(command)}"
                    )
                if line.startswith(READY_PREFIX):
                    return process, line[len(READY_PREFIX) :].strip()
    except TimeoutError:
        process.kill()
        raise ProjectError(
            f"The agent did not print {READY_PREFIX.strip()} within 30 seconds"
        ) from None
    except BaseException:
        process.kill()
        raise


async def _agent_info(client: httpx.AsyncClient, name: str) -> dict[str, Any] | None:
    response = await client.get("/v1/agents")
    response.raise_for_status()
    agents: list[dict[str, Any]] = response.json()["agents"]
    return next((a for a in agents if a["name"] == name), None)


@dataclass(frozen=True)
class InProcessLimits:
    """Overrides for the RunWeave started in-process (bench sweeps and fault profiles)."""

    workers: int | None = None
    fault_profile: str | None = None


DEFAULT_LIMITS = InProcessLimits()


@asynccontextmanager
async def open_target(
    project: Project,
    server: str | None,
    *,
    agent: AgentSection | CandidateSection | None = None,
    limits: InProcessLimits = DEFAULT_LIMITS,
) -> AsyncIterator[Target]:
    section = agent or project.agent
    name = section.name or project.agent.name
    async with AsyncExitStack() as stack:
        agent_url: str | None = section.http
        if section.command is not None:
            process, agent_url = await _start_agent(section.command, project)

            async def stop() -> None:
                if process.returncode is None:
                    process.kill()
                    await process.wait()

            stack.push_async_callback(stop)
        if server is not None:
            client = await stack.enter_async_context(
                httpx.AsyncClient(base_url=server.rstrip("/"), timeout=60, trust_env=False)
            )
            try:
                info = await _agent_info(client, name)
            except httpx.HTTPError as error:
                raise ProjectError(
                    f"Cannot reach RunWeave at {server}: {type(error).__name__}"
                ) from None
            if info is None and agent_url is not None:
                registered = await client.post("/v1/agents/http", json={"url": agent_url})
                if registered.status_code != 201:
                    raise ProjectError(
                        f"The server could not register {agent_url}: {registered.text[:200]}"
                    )
                info = await _agent_info(client, name)
            mode = f"server {server.rstrip('/')}"
        else:
            client, mode = await _in_process(stack, project, section, agent_url, limits)
            info = await _agent_info(client, name)
        if info is None:
            raise ProjectError(
                f"Agent {name!r} is not registered"
                + (
                    " on the server; start it with --agent or connect it first"
                    if server
                    else f"; check [agent] in {project.file.name}"
                )
            )
        yield Target(client, mode, info)


async def _in_process(
    stack: AsyncExitStack,
    project: Project,
    section: AgentSection | CandidateSection,
    agent_url: str | None,
    limits: InProcessLimits,
) -> tuple[httpx.AsyncClient, str]:
    import uvicorn

    from runweave.agents.catalog import AgentLoadError, load_python_agents
    from runweave.app import create_app
    from runweave.core.settings import Settings

    agents = []
    if section.python is not None:
        root = str(project.root)
        if root not in sys.path:
            sys.path.insert(0, root)
        try:
            agents = load_python_agents([section.python])
        except AgentLoadError as error:
            raise ProjectError(str(error)) from None
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    stack.callback(listener.close)
    listener.bind(("127.0.0.1", 0))
    url = f"http://127.0.0.1:{listener.getsockname()[1]}"
    fault_profile = (
        project.proxy.fault_profile if limits.fault_profile is None else limits.fault_profile
    )
    workers = max(4, project.limits.concurrency, limits.workers or 0)
    try:
        settings = Settings(  # type: ignore[call-arg]
            _env_file=None,
            auth_mode="local",
            database_url=None,
            model_provider="fake",
            worker_count=workers,
            queue_capacity=max(1000, workers),
            history_max_records=max(1000, workers * 4),
            event_stream_max_subscribers=min(1024, workers + 8),
            run_timeout_seconds=project.limits.run_timeout_seconds,
            proxy_mode=project.proxy.mode,
            proxy_public_url=url,
            proxy_fault_profile=fault_profile,
            proxy_fault_seed=project.proxy.fault_seed,
            proxy_mock_fixtures=project.mock_fixtures,
            key_store="none",
        )
        app = create_app(settings, agents=agents, http_agents=[agent_url] if agent_url else [])
    except ValueError as error:
        raise ProjectError(str(error)) from None
    server = uvicorn.Server(
        uvicorn.Config(app, log_level="error", access_log=False, timeout_graceful_shutdown=5)
    )
    serving = asyncio.create_task(server.serve(sockets=[listener]), name="runweave-in-process")

    async def stop() -> None:
        server.should_exit = True
        try:
            async with asyncio.timeout(15):
                await asyncio.gather(serving, return_exceptions=True)
        except TimeoutError:
            serving.cancel()
            await asyncio.gather(serving, return_exceptions=True)

    stack.push_async_callback(stop)
    async with asyncio.timeout(30):
        while not server.started:  # uvicorn exposes startup only as a flag
            if serving.done():
                raise ProjectError(
                    "RunWeave could not start in-process; check the agent and runweave.toml"
                )
            await asyncio.sleep(0.01)
    client = await stack.enter_async_context(
        httpx.AsyncClient(
            base_url=url,
            timeout=60,
            trust_env=False,
            limits=httpx.Limits(max_connections=workers + 8, max_keepalive_connections=workers + 8),
        )
    )
    return client, "in-process"


def _ms(start: str | None, end: str | None) -> float:
    if not start or not end:
        return 0.0
    return max(
        0.0, (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds() * 1000
    )


async def run_case(target: Target, payload: dict[str, Any]) -> CaseResult:
    client = target.client
    schema = target.agent.get("output_schema")
    for _ in range(200):
        response = await client.post(
            "/v1/runs", json={"agent": target.agent["name"], "input": payload}
        )
        if response.status_code == 503:
            await asyncio.sleep(float(response.headers.get("retry-after", "0.2") or 0.2))
            continue
        break
    if response.status_code != 202:
        detail = (
            response.json()
            if response.headers.get("content-type", "").startswith("application/json")
            else {}
        ).get("detail")
        code = detail.get("code") if isinstance(detail, dict) else f"HTTP_{response.status_code}"
        return CaseResult(
            {
                "status": "REJECTED",
                "result": None,
                "steps": [],
                "usage": None,
                "error": {"code": code, "message": "Rejected at admission"},
            },
            0.0,
            schema,
        )
    url = response.json()["status_url"]
    delay = 0.01
    while True:
        snapshot: dict[str, Any] = (await client.get(url)).json()
        if snapshot["status"] in TERMINAL:
            return CaseResult(
                snapshot, _ms(snapshot.get("submitted_at"), snapshot.get("finished_at")), schema
            )
        await asyncio.sleep(delay)
        delay = min(0.1, delay * 1.5)
