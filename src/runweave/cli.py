"""`runweave` command line.

`init` creates a project, `eval` tests its agent against a dataset, `bench` load-tests it,
`shadow` compares a candidate version with it, `approvals` decides agents' approval
requests, `serve` starts the local workspace with your agents, and `serve-agent` exposes
Python agents over the HTTP agent contract so another RunWeave can call them.

Exit codes: 0 success, 1 failed gates or a failed command, 2 a project or usage problem.
"""

import argparse
import asyncio
import json
import os
import sys
import threading
from collections.abc import Coroutine, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar

from runweave.agents.catalog import TRIAGE_AGENT, AgentLoadError, load_python_agents
from runweave.core.settings import Settings

if TYPE_CHECKING:
    from runweave.agents.http import HttpAgent

LOOPBACK = "127.0.0.1"
T = TypeVar("T")


def _settings(demo: bool) -> Settings:
    if not demo:
        return Settings()
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        model_provider="fake",
        auth_mode="local",
        database_url=None,
        isolated_tools_enabled=False,
        agent_workflow="baseline",
    )


def bind_host(args: argparse.Namespace) -> str:
    """Loopback, except inside a container image, where the engine publishes the port.

    The container's own network namespace is not reachable from other machines; the
    Compose file publishes it on the host's loopback only, and the workspace keeps its
    Host and origin checks.
    """
    return "0.0.0.0" if args.container else LOOPBACK


def serve(args: argparse.Namespace) -> int:
    if args.host != LOOPBACK:
        print(f"runweave serve binds only to loopback ({LOOPBACK}).", file=sys.stderr)
        return 2
    if not 1 <= args.port <= 65535:
        print("Use a port between 1 and 65535.", file=sys.stderr)
        return 2
    from runweave.agents.http import AgentUrlNotAllowed, normalize_agent_url

    try:
        config = _settings(args.demo)
        if "proxy_public_url" not in config.model_fields_set:
            config.proxy_public_url = f"http://{LOOPBACK}:{args.port}"
        if args.project is not None:
            config.project_file = args.project.resolve()
        elif config.project_file is None and Path("runweave.toml").is_file():
            config.project_file = Path("runweave.toml").resolve()
        # Agents are imported from the working folder (and the project's), as `python -m`
        # would; the installed `runweave` command does not put it on sys.path itself.
        for folder in dict.fromkeys(
            [str(Path.cwd()), *([str(config.project_file.parent)] if config.project_file else [])]
        ):
            if folder not in sys.path:
                sys.path.insert(0, folder)
        agents = load_python_agents([*config.agents, *args.agent])
        http_agents = [
            *config.http_agents,
            *(
                normalize_agent_url(url, allowed_hosts=config.http_agent_allowed_hosts)
                for url in args.http_agent
            ),
        ]
    except (AgentLoadError, AgentUrlNotAllowed, ValueError) as error:
        print(f"runweave: {error}", file=sys.stderr)
        return 1
    if args.check:
        try:
            remote = asyncio.run(_discover(config, http_agents))
        except RuntimeError as error:
            print(f"runweave: {error}", file=sys.stderr)
            return 1
        print(f"{TRIAGE_AGENT} {config.agent_workflow} (built-in)")
        for agent in agents:
            print(f"{agent.info.name} {agent.info.version} (python: {agent.info.target})")
        for item in remote:
            print(f"{item.info.name} {item.info.version} (http: {item.info.target})")
        return 0

    import uvicorn

    from runweave.app import create_app
    from runweave.workspace import create_workspace, workspace_assets

    try:
        app = (
            create_app(config, agents=agents, http_agents=http_agents)
            if args.api_only
            else create_workspace(
                config, workspace_assets(args.assets), agents=agents, http_agents=http_agents
            )
        )
    except ValueError as error:
        print(f"runweave: {error}", file=sys.stderr)
        return 1
    uvicorn.run(
        app, host=bind_host(args), port=args.port, proxy_headers=False, timeout_graceful_shutdown=10
    )
    return 0


async def _discover(config: Settings, urls: Sequence[str]) -> "list[HttpAgent]":
    import httpx

    from runweave.agents.http import HttpAgentConnector, RetryPolicy
    from runweave.core.runtime import connect_startup_agents

    async with httpx.AsyncClient(follow_redirects=False, trust_env=False) as client:
        connector = HttpAgentConnector(
            client,
            allowed_hosts=config.http_agent_allowed_hosts,
            retry=RetryPolicy(1, 0.1, 0.1),
            max_response_bytes=config.http_agent_response_max_bytes,
            connect_timeout=config.http_agent_connect_timeout_seconds,
        )
        return await connect_startup_agents(connector, urls, limit=config.http_agent_max_registered)


def serve_agent(args: argparse.Namespace) -> int:
    if args.host != LOOPBACK:
        print(f"runweave serve-agent binds only to loopback ({LOOPBACK}).", file=sys.stderr)
        return 2
    if not 0 <= args.port <= 65535:
        print("Use a port between 0 (any free port) and 65535.", file=sys.stderr)
        return 2
    try:
        agents = load_python_agents(args.target)
    except AgentLoadError as error:
        print(f"runweave: {error}", file=sys.stderr)
        return 1

    from runweave.sdk_http import create_agent_app, serve_agents

    app = create_agent_app(*(agent.function for agent in agents))
    try:
        asyncio.run(serve_agents(app, host=LOOPBACK, port=args.port))
    except KeyboardInterrupt:
        pass
    except OSError as error:
        print(f"runweave: cannot listen on port {args.port}: {error.strerror}", file=sys.stderr)
        return 1
    return 0


def init(args: argparse.Namespace) -> int:
    from runweave.project import ProjectError
    from runweave.scaffold import init_project

    directory = Path(args.directory)
    try:
        written = init_project(directory, args.language, force=args.force)
    except ProjectError as error:
        print(f"runweave: {error}", file=sys.stderr)
        return 2
    print(f"Created a {args.language} project in {directory} ({len(written)} files).")
    steps = ["npm install", "runweave eval"] if args.language == "typescript" else ["runweave eval"]
    where = "" if directory == Path(".") else f"cd {directory}\n  "
    print("Next:\n  " + where + "\n  ".join(steps))
    return 0


def _run(args: argparse.Namespace, work: Coroutine[Any, Any, T]) -> T:
    """Run a command; with --watch-stdin, stop it cleanly when stdin closes (the workspace's
    cancel), so agent processes and the in-process RunWeave are shut down, not orphaned."""
    if not args.watch_stdin:
        return asyncio.run(work)

    async def main() -> T:
        task = asyncio.ensure_future(work)
        loop = asyncio.get_running_loop()

        def watch() -> None:
            # Raw reads: a buffered stdin read blocked in a daemon thread aborts shutdown.
            try:
                while os.read(0, 4096):
                    pass
            except OSError:
                pass
            try:
                loop.call_soon_threadsafe(task.cancel)
            except RuntimeError:  # the command already finished
                pass

        threading.Thread(target=watch, name="runweave-stdin", daemon=True).start()
        return await task

    return asyncio.run(main())


def _print_event(event: dict[str, Any]) -> None:
    print(json.dumps(event, separators=(",", ":")), flush=True)


def _finish(args: argparse.Namespace, report: dict[str, Any], passed: bool, summary: str) -> int:
    if args.progress:
        _print_event(
            {"event": "report", "kind": report["kind"], "id": report["id"], "passed": passed}
        )
    else:
        print(summary)
    return 0 if passed else 1


def eval_command(args: argparse.Namespace) -> int:
    from runweave.evals.cases import CaseError
    from runweave.evals.command import EvalOptions, run_eval, summary_text
    from runweave.evals.report import silent
    from runweave.project import ProjectError, load_project

    try:
        project = load_project(args.config)
        report, folder = _run(
            args,
            run_eval(
                project,
                EvalOptions(
                    dataset=args.dataset,
                    server=args.server,
                    baseline=args.baseline,
                    save_baseline=args.save_baseline,
                    junit=args.junit,
                    progress=_print_event if args.progress else silent,
                ),
            ),
        )
    except (ProjectError, CaseError) as error:
        print(f"runweave: {error}", file=sys.stderr)
        return 2
    except asyncio.CancelledError:
        print("runweave: cancelled", file=sys.stderr)
        return 130
    return _finish(args, report, report["gates"]["passed"], summary_text(report, folder))


def bench_command(args: argparse.Namespace) -> int:
    from runweave.bench import BenchOptions, run_bench, summary_text
    from runweave.evals.cases import CaseError
    from runweave.evals.report import silent
    from runweave.project import ProjectError, load_project

    if args.concurrency and any(not 1 <= level <= 256 for level in args.concurrency):
        print("runweave: use concurrency levels between 1 and 256", file=sys.stderr)
        return 2
    if args.runs is not None and not 1 <= args.runs <= 10000:
        print("runweave: use between 1 and 10000 runs per level", file=sys.stderr)
        return 2
    if args.soak is not None and not 0 <= args.soak <= 3600:
        print("runweave: use a soak between 0 and 3600 seconds", file=sys.stderr)
        return 2
    try:
        project = load_project(args.config)
        report, folder = _run(
            args,
            run_bench(
                project,
                BenchOptions(
                    dataset=args.dataset,
                    server=args.server,
                    concurrency=tuple(sorted(set(args.concurrency))) if args.concurrency else None,
                    runs=args.runs,
                    fault_profiles=tuple(args.fault_profile) if args.fault_profile else None,
                    soak_seconds=args.soak,
                    progress=_print_event if args.progress else silent,
                ),
            ),
        )
    except (ProjectError, CaseError) as error:
        print(f"runweave: {error}", file=sys.stderr)
        return 2
    except asyncio.CancelledError:
        print("runweave: cancelled", file=sys.stderr)
        return 130
    return _finish(args, report, report["gates"]["passed"], summary_text(report, folder))


def shadow_command(args: argparse.Namespace) -> int:
    from runweave.evals.cases import CaseError
    from runweave.evals.report import silent
    from runweave.evals.shadow import ShadowOptions, run_shadow, summary_text
    from runweave.project import ProjectError, load_project

    try:
        project = load_project(args.config)
        report, folder = _run(
            args,
            run_shadow(
                project,
                ShadowOptions(
                    dataset=args.dataset,
                    server=args.server,
                    candidate=args.candidate,
                    progress=_print_event if args.progress else silent,
                ),
            ),
        )
    except (ProjectError, CaseError) as error:
        print(f"runweave: {error}", file=sys.stderr)
        return 2
    except asyncio.CancelledError:
        print("runweave: cancelled", file=sys.stderr)
        return 130
    return _finish(args, report, report["promotion_eligible"], summary_text(report, folder))


def _project_arguments(command: argparse.ArgumentParser) -> None:
    command.add_argument("--config", type=Path, help="runweave.toml (default: search upward)")
    command.add_argument("--dataset", help="Dataset name (default: the first one)")
    command.add_argument(
        "--server", help="Submit to a running RunWeave, e.g. http://127.0.0.1:8000"
    )
    command.add_argument(
        "--progress",
        action="store_true",
        help="Print progress as JSON lines instead of a summary (used by the workspace)",
    )
    command.add_argument("--watch-stdin", action="store_true", help=argparse.SUPPRESS)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="runweave", description="Run and test your agents locally")
    commands = root.add_subparsers(dest="command", required=True)
    start = commands.add_parser("serve", help="Start the local workspace and API")
    start.add_argument(
        "--agent",
        action="append",
        default=[],
        metavar="MODULE:ATTRIBUTE",
        help="Python agent to load at startup (repeatable), e.g. mypkg.agent:run",
    )
    start.add_argument(
        "--http-agent",
        action="append",
        default=[],
        metavar="URL",
        help="HTTP agent base URL to discover at startup (repeatable), e.g. http://127.0.0.1:9100",
    )
    start.add_argument("--demo", action="store_true", help="Offline, in-memory, ignore .env")
    start.add_argument("--host", default=LOOPBACK, help=argparse.SUPPRESS)
    start.add_argument("--port", type=int, default=8000, help="Port on 127.0.0.1")
    start.add_argument(
        "--assets",
        type=Path,
        help="Built workspace UI (default: the packaged UI, else the checkout's frontend/dist)",
    )
    start.add_argument("--check", action="store_true", help="Load agents, list them and exit")
    start.add_argument("--api-only", action="store_true", help="Serve the API without the UI")
    start.add_argument(
        "--container",
        action="store_true",
        help="Inside the service image only: listen on the container's interfaces",
    )
    start.add_argument(
        "--project",
        type=Path,
        metavar="RUNWEAVE_TOML",
        help="Project for the Load tests, Evals and Shadow screens (default: ./runweave.toml)",
    )
    start.set_defaults(handler=serve)
    create = commands.add_parser("init", help="Create a project with a sample agent and dataset")
    create.add_argument("directory", nargs="?", default=".", help="Folder to create the project in")
    create.add_argument(
        "--language",
        choices=("python", "typescript"),
        default="python",
        help="The sample agent's language",
    )
    create.add_argument("--force", action="store_true", help="Overwrite existing files")
    create.set_defaults(handler=init)
    evaluate = commands.add_parser("eval", help="Run a dataset through the agent and gate on it")
    _project_arguments(evaluate)
    evaluate.add_argument("--baseline", type=Path, help="An earlier report.json to compare with")
    evaluate.add_argument(
        "--save-baseline", action="store_true", help="Keep this report as the dataset's baseline"
    )
    evaluate.add_argument("--junit", type=Path, help="Also write JUnit XML here")
    evaluate.set_defaults(handler=eval_command)
    load = commands.add_parser(
        "bench", help="Load-test the agent: concurrency sweeps, fault profiles and soak runs"
    )
    _project_arguments(load)
    load.add_argument(
        "--concurrency", type=int, nargs="+", metavar="N", help="Levels to sweep, e.g. 1 8 32"
    )
    load.add_argument("--runs", type=int, help="Runs per level")
    load.add_argument(
        "--fault-profile",
        action="append",
        metavar="NAME",
        help='Proxy fault profile (repeatable; "" for none), e.g. flaky',
    )
    load.add_argument("--soak", type=float, metavar="SECONDS", help="Then soak at the top level")
    load.set_defaults(handler=bench_command)
    compare = commands.add_parser(
        "shadow", help="Compare a candidate agent version with the current one on a dataset"
    )
    _project_arguments(compare)
    compare.add_argument(
        "--candidate",
        metavar="TARGET",
        help="module:function of a Python @agent, or an HTTP agent URL ([shadow] candidate)",
    )
    compare.set_defaults(handler=shadow_command)
    from runweave.approvals_cli import add_parser as add_approvals

    add_approvals(commands)
    expose = commands.add_parser(
        "serve-agent", help="Serve Python agents over the RunWeave HTTP agent contract"
    )
    expose.add_argument(
        "target", nargs="+", metavar="MODULE:ATTRIBUTE", help="Python @agent functions to serve"
    )
    expose.add_argument("--port", type=int, default=9100, help="0 picks a free port")
    expose.add_argument("--host", default=LOOPBACK, help=argparse.SUPPRESS)
    expose.set_defaults(handler=serve_agent)
    return root


def main(argv: Sequence[str] | None = None) -> None:
    args = parser().parse_args(argv)
    raise SystemExit(args.handler(args))


if __name__ == "__main__":
    main()
