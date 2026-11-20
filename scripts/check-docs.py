"""Check the docs: every relative link resolves, and the quickstart runs as written.

    uv run python scripts/check-docs.py --links
    uv run python scripts/check-docs.py --quickstart --transcript target/quickstart/transcript.md

The quickstart runs the fenced blocks that follow a `<!-- quickstart:run -->` marker, in
order, in an empty folder, with no provider keys in the environment. A block after
`<!-- quickstart:background URL -->` starts in the background; the next step waits until
URL answers, and the check then submits one run to the workspace's API, as the page's
"try it in the workspace" step does by hand. The only substitution: `git clone` of the
repository clones the checkout under test instead.
"""

import argparse
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = "https://github.com/iam-mkd/RunWeave"
MARKER = re.compile(
    r"<!-- quickstart:(run|background)(?: (\S+))? -->\s*\n```[^\n]*\n(.*?)```", re.S
)
LINK = re.compile(r"!?\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
FENCE = re.compile(r"^(```|~~~).*?^\1", re.S | re.M)
SECRET_NAMES = re.compile(r"(?i)(api_key|token|secret|password)")


@dataclass(frozen=True)
class Step:
    command: str
    background: str | None = None


# Links


def slug(heading: str) -> str:
    """GitHub's anchor for a heading."""
    # Rendered text: code spans keep their content (underscores included), links their text.
    text = re.sub(r"`|\[([^\]]*)\]\([^)]*\)", r"\1", heading.strip().lower())
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def anchors(markdown: str) -> set[str]:
    found: set[str] = set()
    for line in FENCE.sub("", markdown).splitlines():
        match = re.match(r"^#{1,6}\s+(.*?)\s*#*$", line)
        if match:
            base = slug(match[1])
            name, n = base, 0
            while name in found:
                n += 1
                name = f"{base}-{n}"
            found.add(name)
    return found


def broken_links(files: list[Path]) -> list[str]:
    broken = []
    for page in files:
        text = page.read_text(encoding="utf-8")
        prose = re.sub(r"`[^`\n]*`", "", FENCE.sub("", text))
        for target in LINK.findall(prose):
            if re.match(r"^[a-z][a-z0-9+.-]*:", target):
                continue  # https:, mailto: and other schemes
            path_part, _, anchor = target.partition("#")
            resolved = (page.parent / path_part) if path_part else page
            if not resolved.exists():
                broken.append(f"{page}: {target}")
            elif anchor and resolved.suffix == ".md":
                if anchor not in anchors(resolved.read_text(encoding="utf-8")):
                    broken.append(f"{page}: {target}")
    return broken


# The quickstart


def quickstart_steps(markdown: str) -> list[Step]:
    steps = []
    for kind, url, body in MARKER.findall(markdown):
        for line in body.splitlines():
            command = re.sub(r"\s+#.*$", "", line).strip()
            if command:
                steps.append(Step(command, url if kind == "background" else None))
    return steps


def substitute(command: str, checkout: Path) -> str:
    if command.startswith(f"git clone {REPOSITORY}"):
        return f'git clone "{checkout}" RunWeave'
    return command


class Transcript:
    def __init__(self) -> None:
        self.lines = ["# Quickstart transcript", ""]

    def add(self, heading: str, body: str = "") -> None:
        self.lines += [f"## {heading}", ""]
        if body.strip():
            self.lines += ["```text", body.strip()[-6000:], "```", ""]


def _environment() -> dict[str, str]:
    """This environment without provider keys: the quickstart must not need one."""
    return {name: value for name, value in os.environ.items() if not SECRET_NAMES.search(name)}


def _wait(url: str, seconds: float, server: subprocess.Popen[str]) -> None:
    import httpx

    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if server.poll() is not None:
            raise SystemExit(f"The server exited with code {server.returncode}")
        try:
            if httpx.get(url, timeout=2, trust_env=False).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    raise SystemExit(f"{url} did not answer within {seconds:.0f} s")


def _try_a_run(base: str, transcript: Transcript) -> None:
    """What the page asks the reader to do in the workspace, through the same API."""
    import httpx

    with httpx.Client(base_url=base, timeout=30, trust_env=False) as client:
        page = client.get("/")
        assert "<html" in page.text.lower(), "the workspace UI is not served"
        agents = [agent["name"] for agent in client.get("/v1/agents").json()["agents"]]
        assert "word-count" in agents, agents
        run = client.post(
            "/v1/runs", json={"agent": "word-count", "input": {"text": "RunWeave tests agents"}}
        ).json()
        deadline = time.monotonic() + 60
        while (state := client.get(f"/v1/runs/{run['id']}").json())["status"] not in {
            "SUCCEEDED", "FAILED", "TIMED_OUT", "CANCELLED"
        }:  # fmt: skip
            assert time.monotonic() < deadline, state
            time.sleep(0.5)
    transcript.add("Workspace: one run", f"agents: {agents}\nrun: {state['status']} "
                   f"{state.get('result')}")  # fmt: skip
    assert state["status"] == "SUCCEEDED", state


def run_quickstart(page: Path, checkout: Path, work: Path, transcript: Transcript) -> None:
    cwd = work
    servers: list[subprocess.Popen[str]] = []
    environment = _environment()
    try:
        for step in quickstart_steps(page.read_text(encoding="utf-8")):
            command = substitute(step.command, checkout)
            if command.startswith("cd "):
                cwd = (cwd / shlex.split(command[3:])[0]).resolve()
                transcript.add(f"`{command}`", f"now in {cwd.name}")
                continue
            started = time.monotonic()
            if step.background:
                server = subprocess.Popen(
                    command, shell=True, cwd=cwd, env=environment, text=True,  # noqa: S602
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                )  # fmt: skip
                servers.append(server)
                _wait(step.background, 120, server)
                transcript.add(f"`{command}` (background)",
                               f"answered after {time.monotonic() - started:.1f} s")  # fmt: skip
                _try_a_run(step.background.rstrip("/"), transcript)
                continue
            done = subprocess.run(
                command, shell=True, cwd=cwd, env=environment, text=True,  # noqa: S602
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=1200, check=False,
            )  # fmt: skip
            took = time.monotonic() - started
            transcript.add(f"`{command}` ({took:.1f} s, exit {done.returncode})", done.stdout)
            if done.returncode != 0:
                raise SystemExit(f"{command} failed with exit code {done.returncode}")
    finally:
        for server in servers:
            server.terminate()
            try:
                server.wait(timeout=30)
            except subprocess.TimeoutExpired:
                server.kill()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--links", action="store_true", help="Check links and anchors")
    parser.add_argument("--quickstart", action="store_true", help="Run docs/quickstart.md")
    parser.add_argument("--checkout", type=Path, default=ROOT, help="Repository to clone")
    parser.add_argument("--transcript", type=Path, help="Write the quickstart transcript here")
    args = parser.parse_args()
    if args.links:
        broken = broken_links([ROOT / "README.md", *sorted((ROOT / "docs").rglob("*.md"))])
        for item in broken:
            print(f"Broken link: {item}")
        if broken:
            return 1
        print("All links resolve")
    if args.quickstart:
        transcript = Transcript()
        work = Path(tempfile.mkdtemp(prefix="runweave-quickstart-")).resolve()
        started = time.monotonic()
        try:
            run_quickstart(ROOT / "docs" / "quickstart.md", args.checkout.resolve(), work,
                           transcript)  # fmt: skip
            transcript.add("Result", f"completed in {time.monotonic() - started:.0f} s")
        finally:
            if args.transcript:
                args.transcript.parent.mkdir(parents=True, exist_ok=True)
                args.transcript.write_text("\n".join(transcript.lines) + "\n", encoding="utf-8")
        print(f"The quickstart ran in {time.monotonic() - started:.0f} s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
