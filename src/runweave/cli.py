"""`runweave` command line. `serve` starts the local workspace with your agents."""

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from runweave.agents.catalog import TRIAGE_AGENT, AgentLoadError, load_python_agents
from runweave.core.settings import Settings

LOOPBACK = "127.0.0.1"


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


def serve(args: argparse.Namespace) -> int:
    if args.host != LOOPBACK:
        print(f"runweave serve binds only to loopback ({LOOPBACK}).", file=sys.stderr)
        return 2
    if not 1 <= args.port <= 65535:
        print("Use a port between 1 and 65535.", file=sys.stderr)
        return 2
    try:
        config = _settings(args.demo)
        agents = load_python_agents([*config.agents, *args.agent])
    except (AgentLoadError, ValueError) as error:
        print(f"runweave: {error}", file=sys.stderr)
        return 1
    if args.check:
        print(f"{TRIAGE_AGENT} {config.agent_workflow} (built-in)")
        for agent in agents:
            print(f"{agent.info.name} {agent.info.version} (python: {agent.info.target})")
        return 0

    import uvicorn

    from runweave.workspace import create_workspace

    try:
        app = create_workspace(config, Path(args.assets).resolve(), agents=agents)
    except ValueError as error:
        print(f"runweave: {error}", file=sys.stderr)
        return 1
    uvicorn.run(
        app, host=LOOPBACK, port=args.port, proxy_headers=False, timeout_graceful_shutdown=10
    )
    return 0


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
    start.add_argument("--demo", action="store_true", help="Offline, in-memory, ignore .env")
    start.add_argument("--host", default=LOOPBACK, help=argparse.SUPPRESS)
    start.add_argument("--port", type=int, default=8000)
    start.add_argument("--assets", default="frontend/dist", help="Built workspace UI directory")
    start.add_argument("--check", action="store_true", help="Load agents, list them and exit")
    start.set_defaults(handler=serve)
    return root


def main(argv: Sequence[str] | None = None) -> None:
    args = parser().parse_args(argv)
    raise SystemExit(args.handler(args))


if __name__ == "__main__":
    main()
