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
    publishing = {"publish-pypi", "publish-npm", "publish-image", "draft-release"}
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
    for name in publishing:  # nothing is published before the wheel works on three systems
        assert {"build", "install-check"} <= set(_needs(jobs[name])), name
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
    assert set(draft["needs"]) == {"build", "install-check", "desktop"}
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


def _needs(job: dict[str, Any]) -> list[str]:
    needs = job.get("needs", [])
    return [needs] if isinstance(needs, str) else list(needs)


def _run_text(job: dict[str, Any]) -> str:
    return "\n".join(step.get("run", "") for step in job.get("steps", []))


# PyPI (milestone 27)


def test_pypi_is_published_by_trusted_publishing_from_the_verified_files() -> None:
    job = _load(".github/workflows/release.yml")["jobs"]["publish-pypi"]
    assert job["environment"] == "release"
    assert job["permissions"] == {"contents": "read", "id-token": "write"}
    assert "refs/tags/v" in job["if"] and "workflow_dispatch" in job["if"]
    steps = job["steps"]
    (download,) = [s for s in steps if s.get("uses", "").startswith("actions/download-artifact@")]
    assert download["with"]["name"] == "release-files"
    (publish,) = [s for s in steps if "gh-action-pypi-publish" in s.get("uses", "")]
    assert PINNED_ACTION.match(publish["uses"]) and publish["uses"].startswith(
        "pypa/gh-action-pypi-publish@"
    )
    # No API token: the upload authenticates with the job's OIDC identity.
    assert "password" not in publish.get("with", {})
    assert "secrets." not in yaml.safe_dump(job)
    upload_dir = publish["with"]["packages-dir"]
    before = _run_text({"steps": steps[: steps.index(publish)]})
    # The checksums are verified before anything is copied for upload, and exactly the
    # wheel and the sdist are copied.
    assert before.index("sha256sum --check SHA256SUMS") < before.index(upload_dir.rstrip("/"))
    assert "runweave-*-py3-none-any.whl" in before and "runweave-*.tar.gz" in before
    assert 'wc -l)" -eq 2' in before  # the upload folder holds exactly two files


def test_the_built_wheel_is_installed_and_used_on_three_fresh_systems() -> None:
    job = _load(".github/workflows/release.yml")["jobs"]["install-check"]
    assert _needs(job) == ["build"]
    assert "if" not in job  # every pull request, not only releases
    assert set(job.get("permissions", {}).values()) <= {"read"}
    matrix = job["strategy"]["matrix"]
    assert set(matrix["os"]) == {"ubuntu-latest", "windows-latest", "macos-latest"}
    assert set(matrix["installer"]) == {"pip", "uv-tool"}
    steps = job["steps"]
    (download,) = [s for s in steps if s.get("uses", "").startswith("actions/download-artifact@")]
    assert download["with"]["name"] == "release-files"
    run = _run_text(job)
    assert "scripts/check-release.py" in run and "--wheel" in run
    assert "--installer ${{ matrix.installer }}" in run
    assert "frontend" not in run and "npm " not in run  # no Node and no UI build


# Package metadata


def _project() -> dict[str, Any]:
    data: dict[str, Any] = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return data["project"]  # type: ignore[no-any-return]


REPOSITORY = "https://github.com/iam-mkd/RunWeave"


def test_pypi_metadata_describes_and_links_the_project() -> None:
    project = _project()
    assert "agent" in project["description"].lower() and len(project["description"]) <= 120
    assert {"agents", "llm", "evaluation", "load-testing", "testing"} <= set(project["keywords"])
    assert project["readme"] == {"file": "README.md", "content-type": "text/markdown"}
    # PEP 639: the license is the SPDX expression alone, with no license classifier.
    assert project["license"] == "Apache-2.0"
    classifiers = project["classifiers"]
    assert not any(c.startswith("License ::") for c in classifiers)
    for expected in ("Environment :: Console", "Topic :: Software Development :: Testing",
                     "Programming Language :: Python :: 3.13"):  # fmt: skip
        assert expected in classifiers
    urls = project["urls"]
    assert {"Homepage", "Documentation", "Repository", "Issues", "Changelog"} <= set(urls)
    assert all(url.startswith(REPOSITORY) for url in urls.values())


def test_the_wheel_carries_the_ui_and_every_command_a_developer_runs() -> None:
    scripts = _project()["scripts"]
    assert scripts["runweave"] == "runweave.cli:main"
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert '"workspace_ui/**/*"' in text


# The npm package


def _sdk() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(
        (ROOT / "sdk" / "typescript" / "package.json").read_text(encoding="utf-8")
    )
    return data


def test_the_sdk_is_published_under_a_name_the_owner_can_use() -> None:
    # The @runweave npm scope belongs to an unrelated project, so the SDK is unscoped.
    sdk = _sdk()
    assert sdk["name"] == "runweave-agent"
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    assert "@runweave/" not in workflow


def test_npm_metadata_matches_the_repository_for_provenance() -> None:
    sdk = _sdk()
    # npm rejects a provenance publish whose repository.url is not the workflow's repository.
    assert sdk["repository"] == {
        "type": "git",
        "url": f"git+{REPOSITORY}.git",
        "directory": "sdk/typescript",
    }
    assert sdk["homepage"].startswith(REPOSITORY)
    assert sdk["bugs"] == {"url": f"{REPOSITORY}/issues"}
    assert {"runweave", "agents", "llm", "testing"} <= set(sdk["keywords"])
    assert sdk["license"] == "Apache-2.0"
    assert sdk["publishConfig"] == {"access": "public", "provenance": True}
    assert sdk["files"] == ["dist"]


def test_npm_publishing_says_how_the_first_publish_authenticates() -> None:
    job = _load(".github/workflows/release.yml")["jobs"]["publish-npm"]
    text = yaml.safe_dump(job)
    # Trusted publishing is configured on an existing package, so the first publish uses a
    # short-lived token kept in the release environment; later ones use OIDC alone.
    assert "secrets.NPM_TOKEN" in text and "NODE_AUTH_TOKEN" in text
    assert "11.5.1" in text  # the oldest npm with trusted publishing
    assert "sha256sum --check SHA256SUMS" in text
    assert "runweave-agent-" in text


def test_npm_publishes_the_tarball_as_a_file_not_a_github_shorthand() -> None:
    # v0.1.0: `npm publish dist/runweave-agent-0.1.0.tgz` read the path as the GitHub
    # repository "dist/runweave-agent-0.1.0.tgz" and tried to clone it. A path that starts
    # with ./ is always a file to npm.
    job = _load(".github/workflows/release.yml")["jobs"]["publish-npm"]
    publish = [
        line.split()
        for line in _run_text(job).splitlines()
        if line.strip().startswith("npm publish")
    ]
    assert publish, "publish-npm runs npm publish"
    for words in publish:
        tarballs = [word for word in words if word.endswith(".tgz")]
        assert tarballs and all(word.startswith("./") for word in tarballs), words


# Versions


def test_every_version_matches_the_release_tag_check() -> None:
    version = _project()["version"]
    assert _sdk()["version"] == version
    vendored = ROOT / "src/runweave/templates/typescript/vendor/runweave-agent/package.json"
    manifest = json.loads(vendored.read_text(encoding="utf-8"))
    assert (manifest["name"], manifest["version"]) == ("runweave-agent", version)
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert re.search(rf"^## \[{re.escape(version)}\] - \d{{4}}-\d{{2}}-\d{{2}}$", changelog, re.M)
    assert f"[{version}]: {REPOSITORY}/releases/tag/v{version}" in changelog
    build = yaml.safe_dump(_load(".github/workflows/release.yml")["jobs"]["build"])
    assert "pyproject.toml" in build and "sdk/typescript/package.json" in build
    # The desktop installers are named after this version and attached to the same release.
    tauri = json.loads((ROOT / "desktop/src-tauri/tauri.conf.json").read_text(encoding="utf-8"))
    cargo = (ROOT / "desktop/src-tauri/Cargo.toml").read_text(encoding="utf-8")
    assert tauri["version"] == version
    assert re.search(rf'^version = "{re.escape(version)}"$', cargo, re.M)


def test_the_owner_checklist_names_exactly_what_the_workflow_uses() -> None:
    guide = (ROOT / "docs" / "release.md").read_text(encoding="utf-8")
    checklist = guide[guide.index("### Owner checklist") :]
    for expected in (
        "PyPI Project Name: `runweave`", "Owner: `iam-mkd`", "Repository name: `RunWeave`",
        "Workflow name: `release.yml`", "Environment name: `release`", "`NPM_TOKEN`",
        "Prevent self-review", "Ref type **Tag**, pattern `v*`", "Review deployments",
        "Approve and deploy", "Publish release", "disallow tokens",
    ):  # fmt: skip
        assert expected in checklist, expected
    jobs = _load(".github/workflows/release.yml")["jobs"]
    assert {"publish-pypi", "publish-npm", "publish-image", "draft-release"} <= jobs.keys()
    assert "secrets.NPM_TOKEN" in yaml.safe_dump(jobs["publish-npm"])
