"""The service the desktop shell starts: `runweave-desktop-service`.

It starts portable PostgreSQL, applies the migrations, runs the project's Python agents
with the developer's interpreter (see bridge.py) and serves the workspace on a free
loopback port, announced as `RUNWEAVE_DESKTOP_READY <url>` on stdout. It stops when stdin
closes (the shell quit or died) or on SIGINT/SIGTERM, then stops the agents and
PostgreSQL. Errors go to stderr as one line starting with `RUNWEAVE_DESKTOP_ERROR`.
"""

import argparse
import asyncio
import socket
import sys
import threading
from collections.abc import Sequence
from importlib.metadata import version
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from runweave.desktop.bridge import AgentBridge

READY_PREFIX = "RUNWEAVE_DESKTOP_READY"
ERROR_PREFIX = "RUNWEAVE_DESKTOP_ERROR"
LOOPBACK = "127.0.0.1"


class ServiceError(RuntimeError):
    pass


def _resources() -> Path:
    """The frozen app's bundle, or the source checkout."""
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[3]))


def migrate(database_url: str, migrations: Path) -> None:
    from alembic import command
    from alembic.config import Config

    if not (migrations / "env.py").is_file():
        raise ServiceError(f"No migrations in {migrations}")
    config = Config()
    config.set_main_option("script_location", str(migrations))
    config.attributes["database_url"] = database_url
    command.upgrade(config, "head")


def _watch_stdin(loop: asyncio.AbstractEventLoop, stop: asyncio.Event) -> None:
    def read() -> None:
        try:
            while sys.stdin.buffer.read(4096):
                pass
        except (OSError, ValueError):
            pass
        loop.call_soon_threadsafe(stop.set)

    threading.Thread(target=read, name="stdin-watch", daemon=True).start()


async def run(args: argparse.Namespace) -> None:
    from runweave.desktop.bridge import AgentBridge
    from runweave.desktop.postgres import PortablePostgres, extract_binaries

    state: Path = args.state_dir
    if args.postgres is not None:
        binaries = args.postgres.resolve()
    elif args.postgres_archive is not None:
        binaries = await asyncio.to_thread(extract_binaries, args.postgres_archive, state)
    else:
        raise ServiceError("Give --postgres or --postgres-archive")
    database = PortablePostgres(binaries, state)
    bridge: AgentBridge | None = None
    try:
        database_url = await database.start()
        await asyncio.to_thread(
            migrate, database_url, args.migrations or _resources() / "migrations"
        )
        bridge, http_agents, project_file = await _agents(args)
        if bridge is not None:
            http_agents = [await bridge.start()]
        await _serve(args, database_url, project_file, http_agents)
    finally:
        if bridge is not None:
            await bridge.stop()
        await database.stop()


async def _agents(
    args: argparse.Namespace,
) -> tuple["AgentBridge | None", list[str], Path | None]:
    """How the workspace reaches the project's agent: a bridge, a URL, or neither."""
    from runweave.desktop.bridge import AgentBridge
    from runweave.desktop.interpreter import check_interpreter
    from runweave.project import ProjectError, load_project

    if args.project is None:
        return None, [], None
    try:
        project = load_project(args.project.resolve())
    except ProjectError as error:
        raise ServiceError(str(error)) from None
    if project.agent.python is None:
        # An HTTP agent connects directly; one started by `command` runs only for jobs.
        return None, [project.agent.http] if project.agent.http else [], project.file
    if args.python is None:
        raise ServiceError("Choose the Python interpreter that runs your project's agents")
    found = await check_interpreter(args.python, expected=version("runweave"))
    if not found.ok:
        raise ServiceError(found.message)
    return AgentBridge(args.python, project.root, [project.agent.python]), [], project.file


async def _serve(
    args: argparse.Namespace,
    database_url: str,
    project_file: Path | None,
    http_agents: Sequence[str],
) -> None:
    import uvicorn
    from pydantic import SecretStr

    from runweave.core.settings import Settings
    from runweave.workspace import create_workspace, workspace_assets

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind((LOOPBACK, args.port))
    url = f"http://{LOOPBACK}:{sock.getsockname()[1]}"
    try:
        config = Settings(
            _env_file=None,  # type: ignore[call-arg]
            database_url=SecretStr(database_url),
            project_file=project_file,
            job_python=args.python,
            auth_mode="local",
            proxy_public_url=url,
        )
        app = create_workspace(config, workspace_assets(args.assets), http_agents=http_agents)
        server = uvicorn.Server(
            uvicorn.Config(
                app, log_level="warning", proxy_headers=False, timeout_graceful_shutdown=10
            )
        )
        stop = asyncio.Event()
        _watch_stdin(asyncio.get_running_loop(), stop)
        serving = asyncio.create_task(server.serve(sockets=[sock]))
        stopping = asyncio.create_task(stop.wait())
        # uvicorn exposes startup only as a flag.
        while not server.started and not serving.done():  # noqa: ASYNC110
            await asyncio.sleep(0.02)
        if server.started:
            print(f"{READY_PREFIX} {url}", flush=True)
        await asyncio.wait({serving, stopping}, return_when=asyncio.FIRST_COMPLETED)
        server.should_exit = True
        stopping.cancel()
        await serving
    finally:
        sock.close()


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(
        prog="runweave-desktop-service", description=__doc__.splitlines()[0]
    )
    command.add_argument("--state-dir", type=Path, required=True, help="App-data folder")
    source = command.add_mutually_exclusive_group()
    source.add_argument("--postgres", type=Path, help="Unpacked PostgreSQL (with bin/)")
    source.add_argument("--postgres-archive", type=Path, help="PostgreSQL .txz to unpack once")
    command.add_argument("--project", type=Path, help="The project's runweave.toml")
    command.add_argument("--python", type=Path, help="Interpreter for the project's agents")
    command.add_argument("--assets", type=Path, help="Built workspace UI (default: packaged)")
    command.add_argument("--migrations", type=Path, help=argparse.SUPPRESS)
    command.add_argument("--port", type=int, default=0, help="0 picks a free port")
    return command


def main(argv: Sequence[str] | None = None) -> None:
    args = parser().parse_args(argv)
    if not 0 <= args.port <= 65535:
        print(f"{ERROR_PREFIX} Use a port between 0 and 65535.", file=sys.stderr, flush=True)
        raise SystemExit(2)
    args.state_dir = args.state_dir.resolve()
    try:
        args.state_dir.mkdir(parents=True, exist_ok=True)
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass
    except (ServiceError, OSError, RuntimeError, ValueError) as error:
        print(f"{ERROR_PREFIX} {error}".replace("\n", " | "), file=sys.stderr, flush=True)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
