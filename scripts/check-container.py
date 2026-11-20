"""Verify the main service image over real loopback TCP, then stop and remove it.

With --compose, bring up compose.yaml (PostgreSQL, migrations, RunWeave) under a unique
project name instead, check it end to end, and remove it with its volume.
"""

import argparse
import asyncio
import json
import os
import secrets
import socket
import subprocess
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

from runweave.tools.container_runtime import ContainerRuntime

ROOT = Path(__file__).resolve().parents[1]


async def check(image: str) -> dict[str, object]:
    runtime = ContainerRuntime()
    name = f"runweave-release-{uuid4().hex}"
    runtime.names.add(name)
    try:
        code, raw = await runtime.command("image", "inspect", image, "--format", "{{.Id}}")
        assert code == 0
        immutable_image = raw.decode().strip()
        code, _ = await runtime.command(
            "create",
            "--name",
            name,
            "--label",
            f"runweave.isolation={runtime.owner}",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges=true",
            "--memory=256m",
            "--pids-limit=64",
            "--cpus=1",
            "--tmpfs=/tmp:rw,noexec,nosuid,size=1048576",
            "-p",
            "127.0.0.1::8000",
            "-e",
            "RUNWEAVE_MODEL_PROVIDER=fake",
            "-e",
            "RUNWEAVE_AUTH_MODE=local",
            immutable_image,
        )
        assert code == 0
        code, _ = await runtime.command("start", name)
        assert code == 0
        code, raw = await runtime.command(
            "container", "inspect", name, "--format", "{{json .NetworkSettings.Ports}}"
        )
        assert code == 0
        binding = json.loads(raw)["8000/tcp"][0]
        assert binding["HostIp"] == "127.0.0.1"
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{int(binding['HostPort'])}", trust_env=False
        ) as client:
            async with asyncio.timeout(30):
                while True:
                    try:
                        ready = await client.get("/health/ready")
                        if ready.status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    await asyncio.sleep(0.1)
            admitted = await client.post(
                "/v1/runs", json={"service": "payments", "description": "Container fixture"}
            )
            assert admitted.status_code == 202
            async with asyncio.timeout(15):
                while True:
                    snapshot = (await client.get(admitted.headers["Location"])).json()
                    if snapshot["finished_at"]:
                        break
                    await asyncio.sleep(0.05)
            assert snapshot["status"] == "SUCCEEDED" and snapshot["usage"]["requests"] == 3
            assert (await client.get("/metrics")).status_code == 200
        code, _ = await runtime.command("stop", "--time", "10", name)
        assert code == 0
        code, raw = await runtime.command(
            "container",
            "inspect",
            name,
            "--format",
            "{{.State.ExitCode}} {{.State.Running}} {{.Config.User}} {{len .Mounts}}",
        )
        assert code == 0 and raw.strip() == b"0 false 10001:10001 0"
        code, version = await runtime.command("version", "--format", "{{.Server.Version}}")
        assert code == 0
        proof = {
            "passed": True,
            "image": immutable_image,
            "docker_engine": version.decode().strip(),
            "loopback_only": True,
            "native_http": True,
            "fake_run": snapshot,
            "graceful_exit_code": 0,
            "nonroot_user": "10001:10001",
            "host_mounts": 0,
            "paid_model_calls": 0,
        }
    finally:
        await runtime.close()
    proof["cleanup_confirmed"] = not runtime.names
    return proof


def _compose(project: str, env_file: Path, image: str, port: int, *args: str) -> str:
    environment = {**os.environ, "RUNWEAVE_IMAGE": image, "RUNWEAVE_PORT": str(port)}
    completed = subprocess.run(
        ["docker", "compose", "-p", project, "-f", str(ROOT / "compose.yaml"),
         "--env-file", str(env_file), *args],
        capture_output=True, text=True, env=environment, timeout=300, check=False,
    )  # fmt: skip
    if completed.returncode != 0:
        raise RuntimeError(f"docker compose {' '.join(args)} failed: {completed.stderr[-2000:]}")
    return completed.stdout


def _wait(client: httpx.Client, url: str, done: Callable[[dict[str, Any]], bool]) -> dict[str, Any]:
    deadline = time.monotonic() + 60
    while True:
        try:
            response = client.get(url)
            if response.status_code == 200:
                body: dict[str, Any] = response.json()
                if done(body):
                    return body
        except httpx.TransportError:
            pass
        if time.monotonic() > deadline:
            raise TimeoutError(url)
        time.sleep(0.2)


def check_compose(image: str) -> dict[str, object]:
    """compose.yaml end to end: the UI, its guards, a durable approval, and a restart."""
    project = f"runweave-check-{uuid4().hex[:12]}"
    with tempfile.TemporaryDirectory() as folder:
        env_file = Path(folder) / "compose.env"
        env_file.write_text(f"POSTGRES_PASSWORD={secrets.token_hex(24)}\n", encoding="utf-8")
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = int(probe.getsockname()[1])
        try:
            _compose(project, env_file, image, port, "up", "-d", "--wait", "--no-build")
            published = _compose(project, env_file, image, port, "ps", "--format", "json")
            services = [json.loads(line) for line in published.splitlines() if line.strip()]
            bindings = {
                item["Service"]: sorted(
                    {(entry["URL"], entry["PublishedPort"]) for entry in item["Publishers"]
                     if entry.get("PublishedPort")}
                )
                for item in services
            }  # fmt: skip
            assert bindings.get("postgres") == [], bindings
            assert bindings.get("runweave") == [("127.0.0.1", port)], bindings
            with httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False) as client:
                _wait(client, "/health/ready", lambda body: True)
                page = client.get("/")
                assert page.status_code == 200 and '<div id="root">' in page.text
                spoofed = client.get("/health/ready", headers={"Host": "evil.example"})
                assert spoofed.status_code == 400
                foreign = client.post(
                    "/v1/runs", json={}, headers={"Origin": "http://evil.example"}
                )
                assert foreign.status_code == 403
                admitted = client.post(
                    "/v1/runs",
                    json={
                        "service": "payments",
                        "description": "Compose check",
                        "create_ticket": True,
                    },
                )
                assert admitted.status_code == 202, admitted.text
                run_url = admitted.headers["Location"]
                _wait(client, run_url, lambda body: body["status"] == "WAITING_APPROVAL")
                approval = client.get(f"{run_url}/approval").json()
                decided = client.post(
                    f"{run_url}/approval",
                    json={
                        "approval_id": approval["id"],
                        "call_id": approval["call"]["id"],
                        "argument_digest": approval["argument_digest"],
                        "policy_version": approval["policy_version"],
                        "expires_at": approval["expires_at"],
                        "decision": "approve",
                    },
                )
                assert decided.status_code == 200, decided.text
                finished = _wait(client, run_url, lambda body: bool(body["finished_at"]))
                assert finished["status"] == "SUCCEEDED", finished
            _compose(project, env_file, image, port, "restart", "runweave")
            with httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False) as client:
                _wait(client, "/health/ready", lambda body: True)
                kept = client.get(run_url).json()
                assert kept["status"] == "SUCCEEDED" and kept["id"] == finished["id"]
        finally:
            _compose(project, env_file, image, port, "down", "-v", "--remove-orphans")
    return {
        "passed": True,
        "image": image,
        "published": {"runweave": "127.0.0.1 only", "postgres": "not published"},
        "ui_served": True,
        "host_check": 400,
        "cross_origin_check": 403,
        "durable_approval": {"run": finished["id"], "status": finished["status"]},
        "survived_restart": True,
        "paid_model_calls": 0,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="runweave-release:m16")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--compose", action="store_true", help="Check compose.yaml instead")
    args = parser.parse_args()
    result = check_compose(args.image) if args.compose else asyncio.run(check(args.image))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
