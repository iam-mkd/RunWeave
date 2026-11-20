"""PostgreSQL for the desktop app, in the user's app-data folder.

The server listens on 127.0.0.1 only (no Unix sockets), on a free port chosen at each
start, with SCRAM authentication for one `runweave` role whose password is generated at
first start. The password file sits beside the data folder, which only the user can read
(PostgreSQL itself requires that of its data folder), so the OS credential store would
protect nothing more. A server left running by a crash is stopped before starting.
"""

import asyncio
import hashlib
import os
import secrets
import shutil
import socket
import sys
import tarfile
import tempfile
from pathlib import Path

import asyncpg  # type: ignore[import-untyped]

USER = "runweave"
DATABASE = "runweave"
COMMAND_TIMEOUT_SECONDS = 120
_EXE = ".exe" if sys.platform == "win32" else ""


def extract_binaries(archive: Path, state: Path) -> Path:
    """Unpacks a PostgreSQL .txz once into a folder named after its checksum."""
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()[:16]
    target = state / "postgres" / f"binaries-{digest}"
    if (target / ".complete").is_file():
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix="binaries-", dir=target.parent))
    try:
        with tarfile.open(archive, "r:xz") as tar:
            try:
                tar.extractall(staging, filter="data")
            except tarfile.FilterError as error:
                raise ValueError(f"Unsafe path in the PostgreSQL archive: {error}") from None
        (staging / ".complete").write_text(digest, encoding="utf-8")
        if target.exists():
            shutil.rmtree(target)
        staging.rename(target)
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
    return target


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class PostgresError(RuntimeError):
    pass


class PortablePostgres:
    def __init__(self, binaries: Path, state: Path) -> None:
        self._bin = binaries.resolve() / "bin"
        self._folder = state / "postgres"
        self._data = self._folder / "data"
        self._password_file = self._folder / "password"
        self._log = self._folder / "postgres.log"
        self.port = 0

    def _tool(self, name: str) -> str:
        return str(self._bin / f"{name}{_EXE}")

    async def _run(self, *argv: str, capture: bool = True) -> tuple[int, str]:
        # PG* variables from the user's shell must not redirect these commands.
        env = {k: v for k, v in os.environ.items() if not k.upper().startswith("PG")}
        # pg_ctl start leaves the server running; piping its output would keep the pipe
        # open for the server's lifetime, so start writes to the log file instead.
        output = asyncio.subprocess.PIPE if capture else asyncio.subprocess.DEVNULL
        # Run in our own folder: as an administrator, PostgreSQL's tools restart themselves
        # with a restricted token, which may not reach the caller's working directory.
        self._folder.mkdir(parents=True, exist_ok=True)
        process = await asyncio.create_subprocess_exec(
            *argv, env=env, cwd=self._folder, stdin=asyncio.subprocess.DEVNULL, stdout=output,
            stderr=asyncio.subprocess.STDOUT if capture else asyncio.subprocess.DEVNULL,
        )  # fmt: skip
        try:
            async with asyncio.timeout(COMMAND_TIMEOUT_SECONDS):
                stdout, _ = await process.communicate()
        except TimeoutError:
            process.kill()
            await process.wait()
            raise PostgresError(f"{Path(argv[0]).name} did not finish in time") from None
        assert process.returncode is not None
        return process.returncode, (stdout or b"").decode("utf-8", "replace")

    def _log_tail(self) -> str:
        try:
            return "\n".join(
                self._log.read_text(encoding="utf-8", errors="replace").splitlines()[-15:]
            )
        except OSError:
            return ""

    async def _initialize(self) -> None:
        self._folder.mkdir(parents=True, exist_ok=True)
        if not self._password_file.is_file():
            descriptor = os.open(self._password_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(secrets.token_urlsafe(32) + "\n")
        code, output = await self._run(
            self._tool("initdb"), "-D", str(self._data), "-U", USER, "-E", "UTF8", "--no-locale",
            "--auth-host=scram-sha-256", "--auth-local=scram-sha-256",
            f"--pwfile={self._password_file}", "--no-instructions",
        )  # fmt: skip
        if code != 0:
            raise PostgresError(f"initdb failed:\n{output[-2000:]}")
        settings = ["listen_addresses = '127.0.0.1'"]
        if sys.platform != "win32":
            settings.append("unix_socket_directories = ''")
        with (self._data / "postgresql.conf").open("a", encoding="utf-8") as conf:
            conf.write("\n# RunWeave desktop\n" + "\n".join(settings) + "\n")

    async def _grant_current_user(self) -> None:
        """Windows: give the user's own SID full control of the PostgreSQL folder.

        Started by an administrator, initdb and pg_ctl restart themselves with a restricted
        token in which the Administrators group is deny-only. Where a profile folder grants
        access only through that group (as on GitHub's Windows runners), the restricted
        process could not open its folders; an explicit entry for the user's SID fixes it.
        """
        system = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
        code, output = await self._run(str(system / "whoami.exe"), "/user", "/fo", "csv", "/nh")
        sid = output.strip().rsplit(",", 1)[-1].strip('"') if code == 0 else ""
        if not sid.startswith("S-1-"):
            raise PostgresError("Could not read the current user's security identifier")
        code, output = await self._run(
            str(system / "icacls.exe"), str(self._folder), "/grant", f"*{sid}:(OI)(CI)F", "/q"
        )
        if code != 0:
            raise PostgresError(f"Could not set access to {self._folder}: {output.strip()}")

    async def _running(self) -> bool:
        if not (self._data / "postmaster.pid").is_file():
            return False
        code, _ = await self._run(self._tool("pg_ctl"), "-D", str(self._data), "status")
        return code == 0

    async def start(self) -> str:
        """Starts the server and returns its database URL (postgresql+asyncpg)."""
        if sys.platform == "win32":
            await self._grant_current_user()
        if await self._running():
            await self.stop()
        if not (self._data / "PG_VERSION").is_file():
            if self._data.exists():
                shutil.rmtree(self._data)  # an initdb that did not finish
            await self._initialize()
        password = self._password_file.read_text(encoding="utf-8").strip()
        self.port = _free_port()
        code, _ = await self._run(
            self._tool("pg_ctl"), "-D", str(self._data), "-l", str(self._log), "-w",
            "-t", "60", "-o", f"-p {self.port}", "start", capture=False,
        )  # fmt: skip
        if code != 0:
            raise PostgresError(f"PostgreSQL did not start:\n{self._log_tail()}")
        # Loopback only and no TLS configured, so there is nothing to negotiate.
        connection = await asyncpg.connect(
            host="127.0.0.1", port=self.port, user=USER, password=password, database="postgres",
            ssl=False,
        )  # fmt: skip
        try:
            if not await connection.fetchval(
                "select 1 from pg_database where datname = $1", DATABASE
            ):
                await connection.execute(f'create database "{DATABASE}"')
        finally:
            await connection.close()
        return (
            f"postgresql+asyncpg://{USER}:{password}@127.0.0.1:{self.port}/{DATABASE}?ssl=disable"
        )

    async def stop(self) -> None:
        """Fast shutdown (rolls back open transactions), immediate if that fails."""
        if not (self._data / "postmaster.pid").is_file():
            return
        pg_ctl = self._tool("pg_ctl")
        code, _ = await self._run(
            pg_ctl, "-D", str(self._data), "-m", "fast", "-w", "-t", "30", "stop"
        )
        if code != 0 and await self._running():
            await self._run(pg_ctl, "-D", str(self._data), "-m", "immediate", "-w", "stop")
