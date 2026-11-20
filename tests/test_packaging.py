"""Guarantees of the service image, the Compose file and the release workflow."""

import json
import re
import tomllib
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
PINNED_ACTION = re.compile(r"^[\w.-]+/[\w./-]+@[0-9a-f]{40}$")


def _load(path: str) -> dict[Any, Any]:
    data = yaml.safe_load((ROOT / path).read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def test_durable_extra_pins_the_same_versions_as_requirements_durable() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    extra = sorted(project["optional-dependencies"]["durable"])
    lines = (ROOT / "requirements-durable.txt").read_text(encoding="utf-8").split()
    assert extra == sorted(lines)


def test_image_is_pinned_nonroot_and_carries_the_ui_and_durable_mode() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    stages: set[str] = set()
    for line in dockerfile.splitlines():
        if line.startswith("FROM "):
            words = line.split()
            if words[1] not in stages:
                assert "@sha256:" in words[1], line
            if len(words) == 4 and words[2].upper() == "AS":
                stages.add(words[3])
    assert "npm ci" in dockerfile and "bundle-workspace-ui.py" in dockerfile
    assert "--extra durable --extra workflow" in dockerfile
    assert "COPY migrations" in dockerfile and "alembic.ini" in dockerfile
    assert re.search(r"^USER 10001:10001$", dockerfile, re.MULTILINE)
    assert '"--container"' in dockerfile


def test_compose_publishes_loopback_only_and_requires_a_password() -> None:
    compose = _load("compose.yaml")
    services = compose["services"]
    assert {"postgres", "migrate", "runweave"} <= services.keys()
    for name, service in services.items():
        for port in service.get("ports", []):
            assert str(port).startswith("127.0.0.1:"), (name, port)
        assert "privileged" not in service
    assert services["postgres"].get("ports") is None
    text = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    assert "${POSTGRES_PASSWORD:?" in text
    assert services["runweave"]["depends_on"]["migrate"]["condition"] == (
        "service_completed_successfully"
    )
    assert services["migrate"]["depends_on"]["postgres"]["condition"] == "service_healthy"
    assert "healthcheck" in services["postgres"]
    # RunWeave rejects unknown keys in its own .env, so Compose reads a separate file.
    example = (ROOT / "compose.env.example").read_text(encoding="utf-8")
    assert "POSTGRES_PASSWORD=replace_with" in example
    assert "compose.env" in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()


def _triggers(workflow: dict[Any, Any]) -> dict[str, Any]:
    # YAML 1.1 reads the bare key `on` as True.
    triggers = workflow.get("on", workflow.get(True))
    assert isinstance(triggers, dict)
    return triggers


def test_release_workflow_publishes_only_when_the_owner_asks() -> None:
    workflow = _load(".github/workflows/release.yml")
    triggers = _triggers(workflow)
    assert set(triggers) == {"pull_request", "push", "workflow_dispatch"}
    assert triggers["push"] == {"tags": ["v*"]}
    assert workflow["permissions"] == {}
    publishing = {"publish-npm", "publish-image", "draft-release"}
    jobs = workflow["jobs"]
    assert publishing <= jobs.keys()
    for name, job in jobs.items():
        steps = job.get("steps", [])
        for step in steps:
            if "uses" in step:
                assert PINNED_ACTION.match(step["uses"]), (name, step["uses"])
        if name in publishing:
            condition = job["if"]
            assert "refs/tags/v" in condition and "workflow_dispatch" in condition, name
            assert job.get("environment") == "release", name
        else:
            assert set(job.get("permissions", {}).values()) <= {"read"}, name
    assert jobs["publish-npm"]["permissions"] == {"contents": "read", "id-token": "write"}
    assert "--provenance" in yaml.safe_dump(jobs["publish-npm"])
    assert jobs["publish-image"]["permissions"] == {"contents": "read", "packages": "write"}
    draft = yaml.safe_dump(jobs["draft-release"])
    assert "--draft" in draft and "SHA256SUMS" in draft


def test_release_builds_checksums_and_an_sbom() -> None:
    build = yaml.safe_dump(_load(".github/workflows/release.yml")["jobs"]["build"])
    for expected in ("uv build", "npm pack", "sha256sum", "cyclonedx", "docker build"):
        assert expected in build, expected


def test_draft_release_carries_the_desktop_installers_built_for_the_tag() -> None:
    jobs = _load(".github/workflows/release.yml")["jobs"]
    desktop = jobs["desktop"]
    assert desktop["uses"] == "./.github/workflows/desktop.yml"
    assert "refs/tags/v" in desktop["if"] and "workflow_dispatch" in desktop["if"]
    assert desktop["permissions"] == {"contents": "read"}
    assert "refs/tags/" in str(desktop["with"]["ref"])
    draft = jobs["draft-release"]
    assert set(draft["needs"]) == {"build", "desktop"}
    text = yaml.safe_dump(draft)
    for expected in ("desktop-*", ".msi", "-setup.exe", ".dmg", ".deb", ".rpm", ".AppImage",
                     "THIRD_PARTY_NOTICES.txt", "runweave-desktop-rust.cdx.json"):  # fmt: skip
        assert expected in text, expected


def test_desktop_workflow_is_reusable_and_checks_installers_on_clean_runners() -> None:
    workflow = _load(".github/workflows/desktop.yml")
    triggers = _triggers(workflow)
    assert {"pull_request", "workflow_call"} <= set(triggers)
    assert "ref" in triggers["workflow_call"]["inputs"]
    jobs = workflow["jobs"]
    build = yaml.safe_dump(jobs["build"])
    assert "third-party-notices.py" in build and "THIRD_PARTY_NOTICES.txt" in build
    check = jobs["install-check"]
    assert check["needs"] == "build"
    assert set(check["strategy"]["matrix"]["os"]) == {
        "ubuntu-latest",
        "windows-latest",
        "macos-latest",
    }
    steps = yaml.safe_dump(check)
    assert "scripts/check-desktop.py" in steps and "xvfb-run" in steps
    for job in jobs.values():
        for step in job.get("steps", []):
            if "uses" in step:
                assert PINNED_ACTION.match(step["uses"]), step["uses"]


def test_installers_bundle_the_third_party_notices() -> None:
    config = json.loads(
        (ROOT / "desktop" / "src-tauri" / "tauri.conf.json").read_text(encoding="utf-8")
    )
    resources = config["bundle"]["resources"]
    assert resources["../../target/notices/THIRD_PARTY_NOTICES.txt"] == "THIRD_PARTY_NOTICES.txt"
