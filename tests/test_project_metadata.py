import re
import tomllib
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SUPPORTED_PYTHONS = ("3.11", "3.12", "3.13")
SCRIPT_REFERENCE = re.compile(r"scripts[/\\][\w.-]+\.(?:ps1|py)")


def _pyproject() -> dict[str, Any]:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def test_package_supports_python_311_and_newer() -> None:
    project = _pyproject()["project"]

    assert project["requires-python"] == ">=3.11"
    classifiers = set(project["classifiers"])
    for version in SUPPORTED_PYTHONS:
        assert f"Programming Language :: Python :: {version}" in classifiers


def test_static_checks_target_oldest_supported_python() -> None:
    tool = _pyproject()["tool"]

    assert tool["ruff"]["target-version"] == "py311"
    assert tool["mypy"]["python_version"] == "3.11"


def test_distribution_is_apache_2_licensed() -> None:
    project = _pyproject()["project"]

    assert project["license"] == "Apache-2.0"
    assert set(project["license-files"]) == {"LICENSE", "NOTICE"}
    license_text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert "Apache License" in license_text
    assert "Version 2.0, January 2004" in license_text
    assert "RunWeave" in (ROOT / "NOTICE").read_text(encoding="utf-8")


def test_open_source_project_documents_exist() -> None:
    for name in ("CONTRIBUTING.md", "SECURITY.md", "CHANGELOG.md", "docs/licenses.md"):
        assert (ROOT / name).is_file(), name


def test_scripts_referenced_by_documentation_exist() -> None:
    documents = [ROOT / "README.md", ROOT / "PLAN.md", *sorted((ROOT / "docs").glob("*.md"))]
    referenced = {
        match.replace("\\", "/")
        for document in documents
        for match in SCRIPT_REFERENCE.findall(document.read_text(encoding="utf-8"))
    }

    assert referenced
    assert sorted(path for path in referenced if not (ROOT / path).is_file()) == []


def test_ci_checks_every_supported_python_on_linux_and_windows() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    for version in SUPPORTED_PYTHONS:
        assert f"'{version}'" in workflow
    assert "ubuntu-latest" in workflow
    assert "windows-latest" in workflow


def test_ci_audits_python_and_frontend_dependencies() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    assert "pip-audit" in workflow
    assert "npm audit" in workflow
