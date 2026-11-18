"""`runweave` command line.

`serve` starts the local workspace with your agents; `serve-agent` exposes Python agents
over the HTTP agent contract so another RunWeave can call them.
"""

import argparse
import asyncio
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from runweave.agents.catalog import TRIAGE_AGENT, AgentLoadError, load_python_agents
from runweave.core.settings import Settings

if TYPE_CHECKING:
    from runweave.agents.http import HttpAgent

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
    from runweave.agents.http import AgentUrlNotAllowed, normalize_agent_url

    try:
        config = _settings(args.demo)
        if "proxy_public_url" not in config.model_fields_set:
            config.proxy_public_url = f"http://{LOOPBACK}:{args.port}"
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

    from runweave.workspace import create_workspace

    try:
        app = create_workspace(
            config, Path(args.assets).resolve(), agents=agents, http_agents=http_agents
        )
    except ValueError as error:
        print(f"runweave: {error}", file=sys.stderr)
        return 1
    uvicorn.run(
        app, host=LOOPBACK, port=args.port, proxy_headers=False, timeout_graceful_shutdown=10
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
    start.add_argument("--port", type=int, default=8000)
    start.add_argument("--assets", default="frontend/dist", help="Built workspace UI directory")
    start.add_argument("--check", action="store_true", help="Load agents, list them and exit")
    start.set_defaults(handler=serve)
    expose = commands.add_parser(
        "serve-agent", help="Serve Python agents over the RunWeave HTTP agent contract"
    )
    expose.add_argument("target", nargs="+", metavar="MODULE:ATTRIBUTE")
    expose.add_argument("--port", type=int, default=9100, help="0 picks a free port")
    expose.add_argument("--host", default=LOOPBACK, help=argparse.SUPPRESS)
    expose.set_defaults(handler=serve_agent)
    return root


def main(argv: Sequence[str] | None = None) -> None:
    args = parser().parse_args(argv)
    raise SystemExit(args.handler(args))


if __name__ == "__main__":
    main()
