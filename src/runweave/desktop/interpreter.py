"""Checks the developer's Python before the desktop app runs their agents with it."""

import asyncio
import json
import re
from dataclasses import dataclass
from pathlib import Path

PROBE_TIMEOUT_SECONDS = 20
_PROBE = (
    "import json, importlib.metadata as m\n"
    "try:\n    v = m.version('runweave')\nexcept m.PackageNotFoundError:\n    v = None\n"
    "print(json.dumps({'runweave': v}))"
)
_RELEASE = re.compile(r"^(\d+)\.(\d+)")


@dataclass(frozen=True)
class InterpreterCheck:
    ok: bool
    version: str | None
    message: str


def compatible(expected: str, found: str) -> bool:
    """Same major and minor release: the agent contract does not change within one."""
    wanted, have = _RELEASE.match(expected), _RELEASE.match(found)
    return bool(wanted and have and wanted.groups() == have.groups())


def _install_hint(python: Path, expected: str) -> str:
    return f'Install it there: "{python}" -m pip install "runweave=={expected}"'


async def check_interpreter(python: Path, *, expected: str) -> InterpreterCheck:
    """Runs a fixed probe (no shell, no user input) with the chosen interpreter."""
    try:
        process = await asyncio.create_subprocess_exec(
            str(python), "-I", "-c", _PROBE,
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )  # fmt: skip
    except OSError:
        return InterpreterCheck(False, None, f"Python interpreter not found: {python}")
    try:
        async with asyncio.timeout(PROBE_TIMEOUT_SECONDS):
            stdout, _ = await process.communicate()
    except TimeoutError:
        process.kill()
        await process.wait()
        return InterpreterCheck(False, None, f"{python} did not answer within 20 seconds")
    try:
        found = json.loads(stdout.decode("utf-8", "replace").strip().splitlines()[-1])["runweave"]
    except (IndexError, ValueError, KeyError, TypeError):
        return InterpreterCheck(False, None, f"{python} is not a working Python interpreter")
    if found is None:
        return InterpreterCheck(
            False, None, f"RunWeave is not installed in {python}. {_install_hint(python, expected)}"
        )
    if not compatible(expected, found):
        return InterpreterCheck(
            False,
            found,
            f"{python} has RunWeave {found}; this app needs {expected}. "
            f"{_install_hint(python, expected)}",
        )
    return InterpreterCheck(True, found, "")
