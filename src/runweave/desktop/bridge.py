"""Runs the project's Python agents with the developer's own interpreter.

The desktop service is a frozen program that cannot import the developer's code, so it
starts `<python> -m runweave.cli serve-agent <targets>` in the project folder and connects
to it as an HTTP agent on loopback. The child is owned: stopped on shutdown, killed if it
does not stop in time.
"""

import asyncio
import os
import subprocess
import sys
from collections import deque
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from runweave.sdk_http import READY_PREFIX

START_TIMEOUT_SECONDS = 60
STOP_GRACE_SECONDS = 5


class BridgeError(RuntimeError):
    pass


class AgentBridge:
    def __init__(
        self,
        python: Path,
        project_root: Path,
        targets: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self._argv = [str(python), "-m", "runweave.cli", "serve-agent", *targets, "--port", "0"]
        self._cwd = project_root
        # The developer's own environment, minus RunWeave settings meant for the service.
        self._env = {k: v for k, v in os.environ.items() if not k.startswith("RUNWEAVE_")}
        self._env.update({"PYTHONUNBUFFERED": "1", **(env or {})})
        self._process: asyncio.subprocess.Process | None = None
        self._stderr: deque[str] = deque(maxlen=20)
        self._drain: asyncio.Task[None] | None = None

    @property
    def pid(self) -> int | None:
        return self._process.pid if self._process else None

    async def start(self) -> str:
        """Starts the agents and returns their loopback URL."""
        # Its own process group, so console signals meant for the service do not reach it.
        group: dict[str, Any] = (
            {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
            if sys.platform == "win32"
            else {"start_new_session": True}
        )
        try:
            self._process = await asyncio.create_subprocess_exec(
                *self._argv, cwd=self._cwd, env=self._env,
                stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, **group,
            )  # fmt: skip
        except OSError as error:
            raise BridgeError(f"Cannot start {self._argv[0]}: {error.strerror}") from None
        assert self._process.stdout is not None and self._process.stderr is not None
        self._drain = asyncio.create_task(self._read_errors(self._process.stderr))
        try:
            async with asyncio.timeout(START_TIMEOUT_SECONDS):
                while line := await self._process.stdout.readline():
                    text = line.decode("utf-8", "replace").strip()
                    if text.startswith(READY_PREFIX + " "):
                        return text.split(" ", 1)[1]
                await self._process.wait()
        except TimeoutError:
            await self.stop()
            raise BridgeError("Your agents did not start within 60 seconds") from None
        except BaseException:
            await self.stop()
            raise
        await self.stop()
        detail = "\n".join(self._stderr) or f"exit code {self._process.returncode}"
        raise BridgeError(f"Your agents could not start:\n{detail}")

    async def _read_errors(self, stream: asyncio.StreamReader) -> None:
        while line := await stream.readline():
            self._stderr.append(line.decode("utf-8", "replace").rstrip()[:500])

    async def stop(self) -> None:
        process = self._process
        if process is None:
            return
        try:
            if process.returncode is None:
                process.terminate()
                try:
                    async with asyncio.timeout(STOP_GRACE_SECONDS):
                        await process.wait()
                except TimeoutError:
                    process.kill()
                    await process.wait()
        except ProcessLookupError:
            pass
        finally:
            if self._drain is not None:
                await asyncio.gather(self._drain, return_exceptions=True)
