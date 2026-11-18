"""Verify the main service image over real loopback TCP, then stop and remove it."""

import argparse
import asyncio
import json
from pathlib import Path
from uuid import uuid4

import httpx

from runweave.tools.container_runtime import ContainerRuntime


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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="runweave-release:m16")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = asyncio.run(check(args.image))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
