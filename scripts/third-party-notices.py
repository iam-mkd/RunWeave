"""Write the desktop installers' third-party notices and a CycloneDX SBOM of the Rust crates.

    uv export --locked --no-dev --no-emit-project --extra durable --extra workflow \\
        --format requirements-txt > target/notices/requirements.txt
    uv run --no-sync python scripts/third-party-notices.py --out target/notices

THIRD_PARTY_NOTICES.txt covers what the app bundles: the frozen service's Python
packages and the PyInstaller bootloader, the workspace UI's npm packages, the window's Rust
crates, and PostgreSQL. Each entry has its name, version, declared license and the license
files the package ships. A Python package that is not installed is an error, so an
incomplete notice is never written.
"""

import argparse
import json
import re
import subprocess
from collections.abc import Iterable
from importlib import metadata
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
LICENSE_FILE = re.compile(r"^(licen[cs]e|copying|notice|copyright|authors)", re.IGNORECASE)
RULE = "-" * 78

# PostgreSQL's COPYRIGHT file, verbatim from the REL_18_7 tag.
POSTGRESQL_LICENSE = """PostgreSQL Database Management System
(also known as Postgres, formerly known as Postgres95)

Portions Copyright (c) 1996-2026, PostgreSQL Global Development Group

Portions Copyright (c) 1994, The Regents of the University of California

Permission to use, copy, modify, and distribute this software and its
documentation for any purpose, without fee, and without a written agreement
is hereby granted, provided that the above copyright notice and this
paragraph and the following two paragraphs appear in all copies.

IN NO EVENT SHALL THE UNIVERSITY OF CALIFORNIA BE LIABLE TO ANY PARTY FOR
DIRECT, INDIRECT, SPECIAL, INCIDENTAL, OR CONSEQUENTIAL DAMAGES, INCLUDING
LOST PROFITS, ARISING OUT OF THE USE OF THIS SOFTWARE AND ITS
DOCUMENTATION, EVEN IF THE UNIVERSITY OF CALIFORNIA HAS BEEN ADVISED OF THE
POSSIBILITY OF SUCH DAMAGE.

THE UNIVERSITY OF CALIFORNIA SPECIFICALLY DISCLAIMS ANY WARRANTIES,
INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY
AND FITNESS FOR A PARTICULAR PURPOSE.  THE SOFTWARE PROVIDED HEREUNDER IS
ON AN "AS IS" BASIS, AND THE UNIVERSITY OF CALIFORNIA HAS NO OBLIGATIONS TO
PROVIDE MAINTENANCE, SUPPORT, UPDATES, ENHANCEMENTS, OR MODIFICATIONS.
"""


def _entry(
    name: str, version: str, license_name: str | None, texts: Iterable[tuple[str, str]]
) -> str:
    lines = [RULE, f"{name} {version}", f"License: {license_name or 'not declared'}"]
    found = list(texts)
    if not found:
        lines.append("(no license file shipped with the package)")
    for file_name, text in found:
        lines += ["", f"--- {file_name} ---", text.strip()]
    return "\n".join(lines) + "\n"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _license_files(folder: Path) -> list[tuple[str, str]]:
    if not folder.is_dir():
        return []
    return [
        (item.name, _read(item))
        for item in sorted(folder.iterdir())
        if item.is_file() and LICENSE_FILE.match(item.name)
    ]


def requirement_names(requirements: Path) -> list[str]:
    """Packages a `uv export` installs on this platform (environment markers evaluated)."""
    from packaging.requirements import Requirement

    names = []
    for line in _read(requirements).splitlines():
        if not re.match(r"^[A-Za-z0-9]", line):
            continue  # comments, blank lines and --hash continuations
        requirement = Requirement(line.rstrip(" \\"))
        if requirement.marker is None or requirement.marker.evaluate():
            names.append(requirement.name)
    return names


def _canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def python_section(names: Iterable[str], path: list[str] | None = None) -> str:
    """One entry per distribution; raises LookupError for one that is not installed."""
    found = metadata.distributions(path=path) if path is not None else metadata.distributions()
    installed = {_canonical(dist.metadata["Name"]): dist for dist in found}
    entries = []
    for name in sorted({_canonical(item) for item in names}):
        dist = installed.get(name)
        if dist is None:
            raise LookupError(
                f"Python package {name} is not installed; install the release environment first"
            )
        declared = next(
            (values[0] for key in ("License-Expression", "License")
             if (values := dist.metadata.get_all(key))),
            None,
        )  # fmt: skip
        texts = [
            (Path(str(item)).name, item.read_text())
            for item in (dist.files or [])
            if LICENSE_FILE.match(Path(str(item)).name) and ".dist-info" in str(item)
        ]
        entries.append(
            _entry(dist.metadata["Name"], dist.version, declared, ((n, t or "") for n, t in texts))
        )
    return "".join(entries)


def rust_section(cargo_metadata: dict[str, Any]) -> str:
    own = set(cargo_metadata["workspace_members"])
    entries = []
    for package in sorted(
        cargo_metadata["packages"], key=lambda item: (item["name"], item["version"])
    ):
        if package["id"] in own:
            continue
        folder = Path(package["manifest_path"]).parent
        entries.append(
            _entry(
                package["name"], package["version"], package.get("license"), _license_files(folder)
            )
        )
    return "".join(entries)


def rust_cyclonedx(cargo_metadata: dict[str, Any]) -> dict[str, Any]:
    own = set(cargo_metadata["workspace_members"])
    components = []
    for package in sorted(
        cargo_metadata["packages"], key=lambda item: (item["name"], item["version"])
    ):
        if package["id"] in own:
            continue
        component: dict[str, Any] = {
            "type": "library",
            "name": package["name"],
            "version": package["version"],
            "purl": f"pkg:cargo/{package['name']}@{package['version']}",
        }
        if package.get("license"):
            component["licenses"] = [{"expression": package["license"]}]
        components.append(component)
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "version": 1,
        "metadata": {"component": {"type": "application", "name": "runweave-desktop"}},
        "components": components,
    }


def npm_section(root: Path, tree: dict[str, Any]) -> str:
    """Entries for an `npm ls --omit=dev --all --json` tree, read from root/node_modules."""
    seen: dict[tuple[str, str], str] = {}

    def walk(dependencies: dict[str, Any]) -> None:
        for name, info in dependencies.items():
            version = info.get("version", "")
            if (name, version) in seen:
                continue
            folder = root / "node_modules" / name
            manifest = (
                json.loads(_read(folder / "package.json"))
                if (folder / "package.json").is_file()
                else {}
            )
            seen[(name, version)] = _entry(
                name, version, manifest.get("license"), _license_files(folder)
            )
            walk(info.get("dependencies", {}))

    walk(tree.get("dependencies", {}))
    return "".join(seen[key] for key in sorted(seen))


def postgresql_section(version: str) -> str:
    return _entry(
        "PostgreSQL",
        version,
        "PostgreSQL License (binaries packaged by zonky embedded-postgres-binaries, Apache-2.0)",
        [("COPYRIGHT", POSTGRESQL_LICENSE)],
    )


def standard_licenses_section() -> str:
    """Full texts of the licenses that packages without their own license file declare."""
    folder = ROOT / "scripts" / "licenses"
    texts = [
        ("MIT", _read(folder / "MIT.txt")),
        ("BSD-3-Clause", _read(folder / "BSD-3-Clause.txt")),
        ("Zlib", _read(folder / "Zlib.txt")),
        ("Apache-2.0", _read(ROOT / "LICENSE")),
        ("MPL-2.0", _read(folder / "MPL-2.0.txt")),
    ]
    intro = (
        "Packages above marked as shipping no license file are covered by the standard text\n"
        "of the license they declare, reproduced here.\n"
    )
    return intro + "".join(f"\n--- {name} ---\n{text.strip()}\n" for name, text in texts)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=ROOT / "target" / "notices")
    parser.add_argument(
        "--requirements", type=Path, help="uv export of the bundled Python packages"
    )
    args = parser.parse_args()
    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)
    requirements = args.requirements or out / "requirements.txt"
    cargo = json.loads(
        subprocess.run(
            ["cargo", "metadata", "--format-version", "1", "--locked",
             "--manifest-path", str(ROOT / "desktop" / "src-tauri" / "Cargo.toml")],
            check=True, capture_output=True, text=True, encoding="utf-8",
        ).stdout
    )  # fmt: skip
    npm_tree = json.loads(
        subprocess.run(
            "npm ls --omit=dev --all --json", shell=True, cwd=ROOT / "frontend",  # noqa: S602 (fixed command)
            capture_output=True, text=True, encoding="utf-8", check=False,
        ).stdout or "{}"
    )  # fmt: skip
    fetch = _read(ROOT / "scripts" / "fetch-postgres.py")
    postgres_version = re.search(r'^VERSION = "([^"]+)"', fetch, re.MULTILINE)
    sections = [
        ("RunWeave", _read(ROOT / "LICENSE") + "\n" + _read(ROOT / "NOTICE")),
        ("PostgreSQL (bundled database)",
         postgresql_section(postgres_version[1] if postgres_version else "")),
        ("Python packages in the desktop service (and the PyInstaller bootloader)",
         python_section([*requirement_names(requirements), "pyinstaller"])),
        ("Workspace UI (npm)", npm_section(ROOT / "frontend", npm_tree)),
        ("Desktop window (Rust crates)", rust_section(cargo)),
        ("Standard license texts", standard_licenses_section()),
    ]  # fmt: skip
    text = "RunWeave desktop: third-party notices\n\n" + "\n".join(
        f"{'=' * 78}\n{title}\n{'=' * 78}\n{body}" for title, body in sections
    )
    (out / "THIRD_PARTY_NOTICES.txt").write_text(text, encoding="utf-8")
    bom = rust_cyclonedx(cargo)
    (out / "runweave-desktop-rust.cdx.json").write_text(
        json.dumps(bom, indent=2) + "\n", encoding="utf-8"
    )
    crates = len(bom["components"])
    print(f"Wrote {out / 'THIRD_PARTY_NOTICES.txt'} ({len(text) // 1024} KiB, {crates} crates)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
