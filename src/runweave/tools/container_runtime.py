import asyncio
import json
from dataclasses import dataclass
from uuid import uuid4


class IsolationError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__("Isolated tool operation rejected")


@dataclass(frozen=True)
class ContainerPolicy:
    image: str
    browser: bool = False
    seccomp: str | None = None

    def arguments(self, name: str, owner: str) -> list[str]:
        return [
            "create",
            "--name",
            name,
            "--label",
            f"runweave.isolation={owner}",
            "--pull=never",
            "--network=none",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges=true",
            "--init",
            "--ipc=private",
            "--pids-limit",
            "128" if self.browser else "16",
            "--memory",
            "768m" if self.browser else "64m",
            "--memory-swap",
            "768m" if self.browser else "64m",
            "--cpus",
            "1" if self.browser else "0.5",
            "--ulimit",
            "core=0:0",
            "--ulimit",
            "fsize=8388608:8388608" if self.browser else "fsize=65536:65536",
            "--ulimit",
            "nofile=256:256" if self.browser else "nofile=32:32",
            "--ulimit",
            "cpu=10:10" if self.browser else "cpu=1:1",
            "--user",
            "1000:1000" if self.browser else "65532:65532",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=134217728,mode=1777"
            if self.browser
            else "/tmp:rw,noexec,nosuid,size=1048576,mode=1777",
            *(["--security-opt", f"seccomp={self.seccomp}"] if self.seccomp else []),
            "-i",
        ]


class ContainerRuntime:
    def __init__(self) -> None:
        self.owner = uuid4().hex
        self.names: set[str] = set()
        self.healthy = True

    async def command(
        self, *arguments: str, data: bytes = b"", limit: int = 16384, seconds: float = 30
    ) -> tuple[int, bytes]:
        process = await asyncio.create_subprocess_exec(
            "docker",
            *arguments,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=65536,
        )

        async def read(stream: asyncio.StreamReader | None) -> bytes:
            if stream is None:
                return b""
            chunks = bytearray()
            while chunk := await stream.read(4096):
                chunks.extend(chunk)
                if len(chunks) > limit:
                    raise IsolationError("SANDBOX_OUTPUT_LIMIT")
            return bytes(chunks)

        async def write() -> None:
            if process.stdin:
                try:
                    process.stdin.write(data)
                    await process.stdin.drain()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                finally:
                    process.stdin.close()

        output_task = asyncio.create_task(read(process.stdout))
        wait_task = asyncio.create_task(process.wait())
        tasks = (
            output_task,
            asyncio.create_task(read(process.stderr)),
            asyncio.create_task(write()),
            wait_task,
        )
        try:
            async with asyncio.timeout(seconds):
                await asyncio.gather(*tasks)
            return wait_task.result(), output_task.result()
        finally:
            if process.returncode is None:
                process.kill()
            await process.wait()
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def remove(self, name: str) -> None:
        present, owner = await self.command(
            "container",
            "inspect",
            name,
            "--format",
            '{{index .Config.Labels "runweave.isolation"}}',
        )
        if present != 0:
            checked, matches = await self.command(
                "container", "ls", "-a", "--filter", f"name=^/{name}$", "--format", "{{.ID}}"
            )
            if checked != 0 or matches.strip():
                self.healthy = False
                raise IsolationError("SANDBOX_CLEANUP_UNCONFIRMED")
            self.names.discard(name)
            return
        if present == 0 and owner.decode().strip() != self.owner:
            self.healthy = False
            raise IsolationError("SANDBOX_CLEANUP_UNCONFIRMED")
        code, _ = await self.command("rm", "-f", name)
        if code:
            checked, matches = await self.command(
                "container", "ls", "-a", "--filter", f"name=^/{name}$", "--format", "{{.ID}}"
            )
            if checked != 0 or matches.strip():
                self.healthy = False
                raise IsolationError("SANDBOX_CLEANUP_UNCONFIRMED")
        self.names.discard(name)

    async def run(
        self,
        policy: ContainerPolicy,
        payload: dict[str, object],
        *,
        probe: str | None = None,
        seconds: float | None = None,
    ) -> tuple[int, bytes]:
        name = f"runweave-isolated-{uuid4().hex}"
        self.names.add(name)
        args = policy.arguments(name, self.owner)
        if probe is not None:
            args += ["--entrypoint", "python", policy.image, "-I", "-c", probe]
        else:
            args.append(policy.image)
        create = asyncio.create_task(self.command(*args))
        try:
            code, _ = await asyncio.shield(create)
            if code:
                raise IsolationError("SANDBOX_CREATE_FAILED")
            inspected, controls = await self.command(
                "container",
                "inspect",
                name,
                "--format",
                "{{.HostConfig.NetworkMode}} {{.HostConfig.ReadonlyRootfs}} "
                "{{.HostConfig.Privileged}} {{len .Mounts}}",
            )
            if inspected or controls.decode().strip() != "none true false 0":
                raise IsolationError("SANDBOX_MOUNTS_OR_NETWORK_UNVERIFIED")
            return await self.command(
                "start",
                "-a",
                "-i",
                name,
                data=json.dumps(payload).encode(),
                limit=16384,
                seconds=seconds or (15 if policy.browser else 8),
            )
        except TimeoutError:
            raise IsolationError("SANDBOX_TIMEOUT") from None
        finally:

            async def cleanup() -> None:
                try:
                    await create
                except Exception:
                    pass
                try:
                    await self.remove(name)
                except BaseException:
                    self.healthy = False
                    raise

            cleanup_task = asyncio.create_task(cleanup())
            try:
                await asyncio.shield(cleanup_task)
            except asyncio.CancelledError:
                await cleanup_task
                raise

    async def close(self) -> None:
        for name in tuple(self.names):
            await self.remove(name)
