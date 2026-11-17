import asyncio
import hashlib
import json
from pathlib import Path
from typing import TYPE_CHECKING

from runweave.tools.browser_fixtures import ORIGIN
from runweave.tools.container_runtime import IsolationError

if TYPE_CHECKING:
    from runweave.tools.isolated import IsolatedTools

PROFILE_SHA256 = "77cdaf463cf5a605278debc2cfe13d5b09c1d5331fc3d4599b8f9466741c85e4"

BASE_PROBE = """
import json, os, socket, pathlib, resource
status = pathlib.Path('/proc/self/status').read_text()
sock = socket.socket(); sock.settimeout(.2)
try:
    blocked = sock.connect_ex(('1.1.1.1', 53)) != 0
finally:
    sock.close()
try:
    pathlib.Path('/escape-canary').write_text('denied')
    readonly = False
except OSError:
    readonly = True
pathlib.Path('/tmp/probe').write_text('ok'); pathlib.Path('/tmp/probe').unlink()
try:
    os.chroot('/tmp'); chroot_denied = False
except PermissionError:
    chroot_denied = True
quota, period = pathlib.Path('/sys/fs/cgroup/cpu.max').read_text().split()
print(json.dumps(dict(uid=os.getuid(), network_blocked=blocked, readonly=readonly,
    cpu=float(quota)/float(period),
    memory=int(pathlib.Path('/sys/fs/cgroup/memory.max').read_text()),
    pids=int(pathlib.Path('/sys/fs/cgroup/pids.max').read_text()),
    file_limit=resource.getrlimit(resource.RLIMIT_FSIZE)[1], tmp_write=True,
    chroot_denied=chroot_denied,
    no_privileges='NoNewPrivs:\\t1' in status, seccomp='Seccomp:\\t2' in status,
    caps='CapEff:\\t0000000000000000' in status,
    no_socket=not pathlib.Path('/var/run/docker.sock').exists(),
    no_host=not pathlib.Path('/host').exists(), environment=sorted(os.environ))))
"""
MEMORY_PROBE = """
try:
    blocks = [bytearray(16*1024*1024) for _ in range(128)]
except MemoryError:
    raise SystemExit(77)
raise SystemExit(0)
"""
FILE_PROBE = """
try:
    with open('/tmp/large', 'wb') as f: f.write(b'x'*16777216)
except OSError as error:
    raise SystemExit(77 if error.errno == 27 else 78)
raise SystemExit(0)
"""
PROCESS_PROBE = """
import os, signal, time
children=[]; blocked=False
try:
    for _ in range(160):
        try: pid=os.fork()
        except OSError: blocked=True; break
        if pid == 0: time.sleep(30); os._exit(0)
        children.append(pid)
finally:
    for pid in children:
        try: os.kill(pid, signal.SIGKILL)
        except ProcessLookupError: pass
    for pid in children:
        try: os.waitpid(pid,0)
        except ChildProcessError: pass
raise SystemExit(77 if blocked else 0)
"""


async def verify_runtime(tools: "IsolatedTools") -> dict[str, object]:
    raw_profile = await asyncio.to_thread(Path(str(tools.policy(True).seccomp)).read_bytes)
    profile_hash = hashlib.sha256(
        json.dumps(json.loads(raw_profile), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    if profile_hash != PROFILE_SHA256:
        raise IsolationError("SANDBOX_PROFILE_UNVERIFIED")
    code, raw = await tools.runtime.command("version", "--format", "{{json .Server}}")
    if code:
        raise IsolationError("DOCKER_UNAVAILABLE")
    server = json.loads(raw)
    if server.get("Os") != "linux":
        raise IsolationError("LINUX_SANDBOX_REQUIRED")
    proofs: dict[str, object] = {}
    for browser in (False, True):
        policy = tools.policy(browser)
        role = "browser" if browser else "python"
        code, raw = await tools.runtime.command(
            "image", "inspect", policy.image, "--format", "{{json .Config.Labels}}"
        )
        if code or json.loads(raw).get("org.runweave.sandbox") != role + "-v1":
            raise IsolationError("SANDBOX_IMAGE_UNVERIFIED")
        code, raw = await tools.runtime.run(policy, {}, probe=BASE_PROBE)
        info = json.loads(raw) if code == 0 else {}
        if info.get("uid") != (1000 if browser else 65532) or not all(
            info.get(k) is True
            for k in (
                "network_blocked",
                "readonly",
                "no_privileges",
                "seccomp",
                "caps",
                "no_socket",
                "no_host",
                "tmp_write",
                "chroot_denied",
            )
        ):
            raise IsolationError("SANDBOX_ISOLATION_FAILED")
        if (
            info.get("cpu") != (1 if browser else 0.5)
            or info.get("memory") != (768 if browser else 64) * 1024 * 1024
            or info.get("pids") != (128 if browser else 16)
            or info.get("file_limit") != (8388608 if browser else 65536)
        ):
            raise IsolationError("SANDBOX_RESOURCE_LIMIT_FAILED")
        for probe in (MEMORY_PROBE, FILE_PROBE, PROCESS_PROBE):
            code, _ = await tools.runtime.run(policy, {}, probe=probe, seconds=20)
            if code not in (77, 137, 153):
                raise IsolationError("SANDBOX_RESOURCE_LIMIT_FAILED")
        # Check CPU exhaustion on the Python worker; browser uses the same cgroup/rlimit controls.
        if not browser:
            code, _ = await tools.runtime.run(policy, {}, probe="while True: pass", seconds=8)
            if code not in (137, 152):
                raise IsolationError("SANDBOX_CPU_LIMIT_FAILED")
        proofs[role] = {"image": policy.image, "kernel_checks": info, "limits_verified": True}
    code, raw = await tools.runtime.run(
        tools.policy(), {"code": "result=sum(range(10))", "data": None}
    )
    if code or json.loads(raw).get("result") != 45:
        raise IsolationError("PYTHON_WORKER_UNVERIFIED")
    resources = await tools.broker.bundle("verification", private=False)
    code, raw = await tools.runtime.run(
        tools.policy(True), {"url": ORIGIN + "/", "origins": [ORIGIN], "resources": resources}
    )
    if code or json.loads(raw).get("text") != "RunWeave browser fixture":
        raise IsolationError("BROWSER_SANDBOX_UNVERIFIED")
    if tools.runtime.names:
        raise IsolationError("SANDBOX_CLEANUP_UNCONFIRMED")
    return {
        "policy_version": "isolated-tools-v1",
        "browser_seccomp_sha256": profile_hash,
        "docker_version": server["Version"],
        "kernel": server.get("KernelVersion"),
        "workers": proofs,
        "passed": True,
    }
