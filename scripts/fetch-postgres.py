"""Download the pinned portable PostgreSQL build for this platform and check its SHA-256.

The desktop app bundles the .txz (`--archive`), and the service unpacks it into app-data on
first start. `--out` also unpacks it for tests (RUNWEAVE_TEST_POSTGRES_BINARIES).
Builds come from zonky's embedded-postgres-binaries (PostgreSQL License binaries,
Apache-2.0 packaging) on Maven Central.
"""

import argparse
import hashlib
import io
import platform
import shutil
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

VERSION = "18.7.0"
# Jar SHA-256, checked against Maven Central's SHA-1 when pinned (2026-11-20).
PINS = {
    "windows-amd64": "789d0261833f6c1fc9ea9e0a882ba5ef5d49366144516a44ed6fd7461d08c3f8",
    "linux-amd64": "10a1954a80519c634b59a9c452c3a4c5ea24c409fa0a65ba28b76f36157edecc",
    "linux-arm64v8": "762b7e43792918748e1e19c027f3ea2b294e8574c58a110faeef0e239affac0f",
    "darwin-amd64": "b30057b89dcf6d9c45b22d1f9acddd1a0c8999193d00e7fad8058b6863c6620b",
    "darwin-arm64v8": "42b3812af4fd991ba3b06378590069b082a5912c77ee35b54b25800fcfc52af1",
}
MAVEN = "https://repo1.maven.org/maven2/io/zonky/test/postgres"
MAX_BYTES = 200 * 1024 * 1024


def current_platform() -> str:
    system = {"win32": "windows", "linux": "linux", "darwin": "darwin"}[sys.platform]
    machine = platform.machine().lower()
    arch = "arm64v8" if machine in ("arm64", "aarch64") else "amd64"
    return f"{system}-{arch}"


def fetch(name: str) -> bytes:
    if name not in PINS:
        raise SystemExit(f"No pinned PostgreSQL build for {name}")
    artifact = f"embedded-postgres-binaries-{name}"
    url = f"{MAVEN}/{artifact}/{VERSION}/{artifact}-{VERSION}.jar"
    with urllib.request.urlopen(url, timeout=120) as response:  # noqa: S310 (fixed https URL)
        jar = response.read(MAX_BYTES + 1)
    if len(jar) > MAX_BYTES:
        raise SystemExit("The download is larger than expected")
    digest = hashlib.sha256(jar).hexdigest()
    if digest != PINS[name]:
        raise SystemExit(f"SHA-256 mismatch for {artifact}: {digest}")
    with zipfile.ZipFile(io.BytesIO(jar)) as archive:
        [member] = [item for item in archive.namelist() if item.endswith(".txz")]
        return archive.read(member)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--platform", default=None, help="for example windows-amd64")
    parser.add_argument("--archive", type=Path, help="Write the .txz here")
    parser.add_argument("--out", type=Path, help="Unpack the binaries into this folder")
    args = parser.parse_args()
    if args.archive is None and args.out is None:
        parser.error("give --archive, --out or both")
    txz = fetch(args.platform or current_platform())
    if args.archive is not None:
        args.archive.parent.mkdir(parents=True, exist_ok=True)
        args.archive.write_bytes(txz)
    if args.out is not None:
        if args.out.exists():
            shutil.rmtree(args.out)
        args.out.mkdir(parents=True)
        with tarfile.open(fileobj=io.BytesIO(txz), mode="r:xz") as tar:
            tar.extractall(args.out, filter="data")
    print(f"PostgreSQL {VERSION} ({args.platform or current_platform()}) ready")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
