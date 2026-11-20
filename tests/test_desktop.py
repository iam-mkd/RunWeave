"""The desktop sidecar: portable PostgreSQL, the agent bridge and the service entry point.

PostgreSQL tests need PostgreSQL binaries (a folder with bin/initdb, bin/pg_ctl and
bin/postgres): set RUNWEAVE_TEST_POSTGRES_BINARIES, or run scripts/fetch-postgres.py.
"""

import asyncio
import io
import json
import os
import socket
import subprocess
import sys
import tarfile
from importlib.metadata import version
from pathlib import Path

import asyncpg  # type: ignore[import-untyped]
import httpx
import psutil  # type: ignore[import-untyped]
import pytest

from runweave.app import create_app
from runweave.core.settings import Settings
from runweave.desktop.bridge import AgentBridge, BridgeError
from runweave.desktop.interpreter import check_interpreter, compatible
from runweave.desktop.postgres import PortablePostgres, extract_binaries

ROOT = Path(__file__).resolve().parents[1]
SAMPLE_AGENTS = ROOT / "examples" / "sample-agent"
RUNWEAVE_VERSION = version("runweave")


def _binaries() -> Path | None:
    configured = os.environ.get("RUNWEAVE_TEST_POSTGRES_BINARIES")
    candidates = [Path(configured)] if configured else [ROOT / "target" / "postgres"]
    for folder in candidates:
        if (folder / "bin").is_dir():
            return folder
    return None


needs_postgres = pytest.mark.skipif(
    _binaries() is None, reason="set RUNWEAVE_TEST_POSTGRES_BINARIES to PostgreSQL binaries"
)


def _port_open(port: int) -> bool:
    with socket.socket() as probe:
        probe.settimeout(0.5)
        return probe.connect_ex(("127.0.0.1", port)) == 0


# Jobs and the developer's interpreter


def test_jobs_run_with_the_configured_interpreter(tmp_path: Path) -> None:
    python = tmp_path / "python.exe"
    app = create_app(Settings(_env_file=None, job_python=python))  # type: ignore[call-arg]
    assert app.state.jobs.python == str(python)
    assert create_app(Settings(_env_file=None)).state.jobs.python == sys.executable  # type: ignore[call-arg]


async def test_interpreter_check_reports_the_installed_runweave() -> None:
    found = await check_interpreter(Path(sys.executable), expected=RUNWEAVE_VERSION)
    assert found.ok and found.version == RUNWEAVE_VERSION and found.message == ""


async def test_interpreter_check_explains_a_missing_interpreter_or_package(tmp_path: Path) -> None:
    missing = await check_interpreter(tmp_path / "no-python", expected=RUNWEAVE_VERSION)
    assert not missing.ok and "not found" in missing.message
    venv = tmp_path / "venv"
    await asyncio.to_thread(
        subprocess.run, [sys.executable, "-m", "venv", "--without-pip", str(venv)], check=True
    )
    python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    bare = await check_interpreter(python, expected=RUNWEAVE_VERSION)
    assert not bare.ok and bare.version is None
    assert f"runweave=={RUNWEAVE_VERSION}" in bare.message and str(python) in bare.message


def test_versions_are_compatible_within_a_minor_release() -> None:
    assert compatible("0.1.0", "0.1.3")
    assert compatible("1.4.0rc1", "1.4.2")
    assert not compatible("0.1.0", "0.2.0")
    assert not compatible("1.0.0", "2.0.0")


# Unpacking the PostgreSQL archive


def _archive(path: Path, members: dict[str, bytes]) -> Path:
    with tarfile.open(path, "w:xz") as archive:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o755
            archive.addfile(info, io.BytesIO(data))
    return path


def test_binaries_unpack_once_into_a_versioned_folder(tmp_path: Path) -> None:
    archive = _archive(tmp_path / "pg.txz", {"bin/initdb": b"x", "share/a.sql": b"y"})
    first = extract_binaries(archive, tmp_path / "state")
    assert (first / "bin" / "initdb").read_bytes() == b"x"
    (first / "bin" / "initdb").write_bytes(b"kept")
    assert extract_binaries(archive, tmp_path / "state") == first
    assert (first / "bin" / "initdb").read_bytes() == b"kept"


def test_binaries_refuse_paths_outside_the_folder(tmp_path: Path) -> None:
    archive = _archive(tmp_path / "evil.txz", {"bin/initdb": b"x", "../escape": b"!"})
    with pytest.raises(ValueError):
        extract_binaries(archive, tmp_path / "state")
    assert not (tmp_path / "escape").exists()


# The agent bridge


def _bridge_env() -> dict[str, str]:
    return {"PYTHONPATH": os.pathsep.join([str(ROOT / "src"), str(SAMPLE_AGENTS)])}


async def test_bridge_serves_project_agents_with_the_chosen_interpreter(tmp_path: Path) -> None:
    bridge = AgentBridge(
        Path(sys.executable), tmp_path, ["sample_agent:text_report"], env=_bridge_env()
    )
    url = await bridge.start()
    process = psutil.Process(bridge.pid)
    try:
        assert url.startswith("http://127.0.0.1:")
        async with httpx.AsyncClient(trust_env=False) as client:
            manifest = (await client.get(f"{url}/runweave/v1/agents")).json()
        assert [agent["name"] for agent in manifest["agents"]] == ["text-report"]
    finally:
        await bridge.stop()
    assert not process.is_running() or process.status() == psutil.STATUS_ZOMBIE


async def test_bridge_reports_an_agent_that_cannot_load(tmp_path: Path) -> None:
    bridge = AgentBridge(Path(sys.executable), tmp_path, ["missing_module:run"], env=_bridge_env())
    with pytest.raises(BridgeError, match="could not be imported"):
        await bridge.start()
    assert bridge.pid is None or not psutil.pid_exists(bridge.pid)


# Portable PostgreSQL


@needs_postgres
async def test_postgres_starts_on_loopback_with_a_generated_password(tmp_path: Path) -> None:
    binaries = _binaries()
    assert binaries is not None
    server = PortablePostgres(binaries, tmp_path / "state")
    url = await server.start()
    try:
        password = (tmp_path / "state" / "postgres" / "password").read_text().strip()
        assert len(password) >= 32 and password in url and url.endswith("?ssl=disable")
        if os.name != "nt":
            assert (tmp_path / "state" / "postgres" / "password").stat().st_mode & 0o077 == 0
        connection = await asyncpg.connect(
            host="127.0.0.1", port=server.port, user="runweave", password=password,
            database="runweave",
        )  # fmt: skip
        try:
            assert await connection.fetchval("show listen_addresses") == "127.0.0.1"
            await connection.execute("create table kept (value int); insert into kept values (7)")
        finally:
            await connection.close()
        with pytest.raises(
            (asyncpg.InvalidPasswordError, asyncpg.InvalidAuthorizationSpecificationError)
        ):
            await asyncpg.connect(
                host="127.0.0.1", port=server.port, user="runweave", password="wrong",
                database="runweave",
            )  # fmt: skip
    finally:
        await server.stop()
    assert not _port_open(server.port)
    assert not (tmp_path / "state" / "postgres" / "data" / "postmaster.pid").exists()

    again = PortablePostgres(binaries, tmp_path / "state")
    await again.start()
    try:
        connection = await asyncpg.connect(
            host="127.0.0.1", port=again.port, user="runweave", password=password,
            database="runweave",
        )  # fmt: skip
        try:
            assert await connection.fetchval("select value from kept") == 7
        finally:
            await connection.close()
    finally:
        await again.stop()


@needs_postgres
async def test_postgres_left_running_by_a_crash_is_stopped_first(tmp_path: Path) -> None:
    binaries = _binaries()
    assert binaries is not None
    crashed = PortablePostgres(binaries, tmp_path / "state")
    await crashed.start()
    old_port = crashed.port
    fresh = PortablePostgres(binaries, tmp_path / "state")
    try:
        await fresh.start()
        assert _port_open(fresh.port)
        assert fresh.port == old_port or not _port_open(old_port)
    finally:
        await fresh.stop()
    assert not _port_open(fresh.port)


# The service the desktop shell starts


def _fake_ui(folder: Path) -> Path:
    (folder / "assets").mkdir(parents=True)
    (folder / "index.html").write_text('<div id="root"></div>', encoding="utf-8")
    return folder


@needs_postgres
def test_service_runs_durably_with_the_project_agent_and_cleans_up(tmp_path: Path) -> None:
    binaries = _binaries()
    assert binaries is not None
    project = tmp_path / "demo"
    env = {k: v for k, v in os.environ.items() if not k.startswith("RUNWEAVE_")}
    env["PYTHONPATH"] = str(ROOT / "src")
    env["RUNWEAVE_KEY_STORE"] = "memory"
    subprocess.run(
        [sys.executable, "-m", "runweave.cli", "init", str(project)], env=env, check=True,
        capture_output=True,
    )  # fmt: skip
    service = subprocess.Popen(
        [
            sys.executable, "-m", "runweave.desktop.service",
            "--state-dir", str(tmp_path / "state"),
            "--postgres", str(binaries),
            "--project", str(project / "runweave.toml"),
            "--python", sys.executable,
            "--assets", str(_fake_ui(tmp_path / "ui")),
        ],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
        text=True, cwd=tmp_path,
    )  # fmt: skip
    try:
        assert service.stdout is not None and service.stdin is not None
        ready = service.stdout.readline()
        assert ready.startswith("RUNWEAVE_DESKTOP_READY "), (
            ready + (service.stderr or io.StringIO()).read()
        )
        base = ready.split()[1]
        children = [child.pid for child in psutil.Process(service.pid).children(recursive=True)]
        with httpx.Client(base_url=base, trust_env=False, timeout=10) as client:
            assert client.get("/health/ready").status_code == 200
            assert client.get("/v1/workspace").json()["durable"] is True
            agents = {agent["name"]: agent for agent in client.get("/v1/agents").json()["agents"]}
            assert agents["word-count"]["kind"] == "http"
            assert client.get("/v1/jobs/project").json()["agent"]["name"] == "word-count"
            admitted = client.post(
                "/v1/runs", json={"agent": "word-count", "input": {"text": "one two"}}
            )
            assert admitted.status_code == 202, admitted.text
            run_url = admitted.headers["Location"]
            for _ in range(200):
                run = client.get(run_url).json()
                if run["finished_at"]:
                    break
                asyncio.run(asyncio.sleep(0.05))
            assert run["status"] == "SUCCEEDED", json.dumps(run)
        service.stdin.close()  # the shell went away
        assert service.wait(timeout=60) == 0
    finally:
        if service.poll() is None:
            service.kill()
            service.wait()
    leftovers = [
        pid
        for pid in children
        if psutil.pid_exists(pid) and psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    ]
    assert leftovers == []
