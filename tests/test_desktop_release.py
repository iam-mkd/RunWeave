"""The desktop release helpers: third-party notices, the Rust SBOM and the install check."""

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]
CRATES_IO = "registry+https://github.com/rust-lang/crates.io-index"


def _script(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        name.replace("-", "_"), ROOT / "scripts" / f"{name}.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


notices = _script("third-party-notices")
check = _script("check-desktop")


def _dist(site: Path, name: str, version: str, license_text: str | None) -> None:
    info = site / f"{name}-{version}.dist-info"
    (info / "licenses").mkdir(parents=True)
    files = [f"{name}-{version}.dist-info/METADATA"]
    if license_text is not None:
        (info / "licenses" / "LICENSE").write_text(license_text, encoding="utf-8")
        files.append(f"{name}-{version}.dist-info/licenses/LICENSE")
    (info / "METADATA").write_text(
        f"Metadata-Version: 2.4\nName: {name}\nVersion: {version}\nLicense-Expression: MIT\n",
        encoding="utf-8",
    )
    (info / "RECORD").write_text("\n".join(f"{item},," for item in files) + "\n", encoding="utf-8")


def test_python_notices_include_each_package_and_its_license_text(tmp_path: Path) -> None:
    _dist(tmp_path, "alpha", "1.2.0", "Alpha license text")
    _dist(tmp_path, "beta", "0.3", None)
    section = notices.python_section(["Beta", "alpha"], path=[str(tmp_path)])
    assert section.index("alpha 1.2.0") < section.index("beta 0.3")
    assert "License: MIT" in section and "Alpha license text" in section
    assert "beta 0.3" in section and "no license file shipped" in section
    with pytest.raises(LookupError):
        notices.python_section(["missing-package"], path=[str(tmp_path)])


def _metadata(tmp_path: Path) -> dict[str, object]:
    crate = tmp_path / "serde-1.0.0"
    crate.mkdir()
    (crate / "LICENSE-MIT").write_text("MIT text for serde", encoding="utf-8")
    (crate / "LICENSE-APACHE").write_text("Apache text for serde", encoding="utf-8")
    own = tmp_path / "app"
    own.mkdir()
    return {
        "workspace_members": ["path+file:///app#runweave-desktop@0.1.0"],
        "packages": [
            {"id": "path+file:///app#runweave-desktop@0.1.0", "name": "runweave-desktop",
             "version": "0.1.0", "license": "Apache-2.0",
             "manifest_path": str(own / "Cargo.toml"), "source": None},
            {"id": f"{CRATES_IO}#serde@1.0.0", "name": "serde", "version": "1.0.0",
             "license": "MIT OR Apache-2.0", "manifest_path": str(crate / "Cargo.toml"),
             "source": CRATES_IO},
        ],
    }  # fmt: skip


def test_rust_notices_list_crates_with_their_license_files(tmp_path: Path) -> None:
    section = notices.rust_section(_metadata(tmp_path))
    assert "serde 1.0.0" in section and "License: MIT OR Apache-2.0" in section
    assert "MIT text for serde" in section and "Apache text for serde" in section
    assert "runweave-desktop" not in section


def test_rust_sbom_is_cyclonedx_with_purls_and_licenses(tmp_path: Path) -> None:
    bom = notices.rust_cyclonedx(_metadata(tmp_path))
    assert bom["bomFormat"] == "CycloneDX" and bom["specVersion"] == "1.5"
    [component] = bom["components"]
    assert component["purl"] == "pkg:cargo/serde@1.0.0"
    assert component["licenses"] == [{"expression": "MIT OR Apache-2.0"}]
    json.dumps(bom)


def test_npm_notices_walk_the_production_tree(tmp_path: Path) -> None:
    modules = tmp_path / "node_modules"
    for name, text in (("react", "React MIT text"), ("scheduler", None)):
        package = modules / name
        package.mkdir(parents=True)
        (package / "package.json").write_text(
            json.dumps({"name": name, "license": "MIT"}), encoding="utf-8"
        )
        if text:
            (package / "LICENSE").write_text(text, encoding="utf-8")
    tree = {
        "dependencies": {
            "react": {"version": "19.0.0", "dependencies": {"scheduler": {"version": "0.25.0"}}}
        }
    }
    section = notices.npm_section(tmp_path, tree)
    assert "react 19.0.0" in section and "React MIT text" in section
    assert "scheduler 0.25.0" in section


def test_postgresql_notice_carries_its_license_text() -> None:
    text = notices.postgresql_section("18.7.0")
    assert "PostgreSQL 18.7.0" in text and "PostgreSQL License" in text
    assert "Permission to use, copy, modify, and distribute this software" in text


@pytest.mark.parametrize(
    ("platform", "config", "data"),
    [
        ("win32", "C:/Users/a/AppData/Roaming/dev.runweave.desktop",
         "C:/Users/a/AppData/Local/dev.runweave.desktop"),
        ("darwin", "/Users/a/Library/Application Support/dev.runweave.desktop",
         "/Users/a/Library/Application Support/dev.runweave.desktop"),
        ("linux", "/home/a/.config/dev.runweave.desktop",
         "/home/a/.local/share/dev.runweave.desktop"),
    ],
)  # fmt: skip
def test_app_folders_follow_each_platform(platform: str, config: str, data: str) -> None:
    home = {"win32": "C:/Users/a", "darwin": "/Users/a", "linux": "/home/a"}[platform]
    env = {"APPDATA": "C:/Users/a/AppData/Roaming", "LOCALAPPDATA": "C:/Users/a/AppData/Local"}
    folders = check.app_folders(platform, Path(home), env)
    assert folders == (Path(config), Path(data))


def test_linux_folders_respect_xdg_settings() -> None:
    env = {"XDG_CONFIG_HOME": "/cfg", "XDG_DATA_HOME": "/data"}
    assert check.app_folders("linux", Path("/home/a"), env) == (
        Path("/cfg/dev.runweave.desktop"),
        Path("/data/dev.runweave.desktop"),
    )


def test_requirements_skip_packages_this_platform_does_not_install(tmp_path: Path) -> None:
    requirements = tmp_path / "requirements.txt"
    requirements.write_text(
        "# uv export\n"
        "alpha==1.0 \\\n    --hash=sha256:00\n"
        "beta==2.0 ; python_full_version < '3.0'\n"
        "gamma==3.0 ; sys_platform == 'never'\n"
        "delta==4.0 ; python_full_version >= '3.11'\n",
        encoding="utf-8",
    )
    assert notices.requirement_names(requirements) == ["alpha", "delta"]


def test_standard_license_texts_cover_packages_without_their_own_file() -> None:
    text = notices.standard_licenses_section()
    for heading in ("MIT", "BSD-3-Clause", "Zlib", "Apache-2.0", "MPL-2.0"):
        assert f"--- {heading} ---" in text
    assert "Permission is hereby granted, free of charge" in text
    assert "Mozilla Public License Version 2.0" in text
    assert "This software is provided 'as-is'" in text
