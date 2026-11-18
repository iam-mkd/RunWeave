from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from runweave.core.settings import Settings
from runweave.workspace import create_workspace


@pytest.fixture
def assets(tmp_path: Path) -> Path:
    root = tmp_path / "frontend"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text("<!doctype html><title>RunWeave</title>", encoding="utf-8")
    (root / "assets" / "app.js").write_text('console.log("local")', encoding="utf-8")
    return root


async def test_local_workspace_serves_ui_and_real_run_api_without_secrets(assets: Path) -> None:
    app = create_workspace(
        Settings(worker_count=1, model_api_key=SecretStr("private-workspace-canary")), assets
    )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000"
        ) as client,
    ):
        index = await client.get("/")
        assert index.status_code == 200 and "RunWeave" in index.text
        assert "connect-src 'self'" in index.headers["content-security-policy"]
        assert (await client.get("/assets/app.js")).status_code == 200
        config = (await client.get("/v1/workspace")).json()
        assert config == {
            "provider": "fake",
            "model": "fake-v1",
            "workflow": "baseline",
            "durable": False,
        }
        submitted = await client.post(
            "/v1/runs",
            json={"service": "payments", "description": "Workspace fixture"},
            headers={"Origin": "http://127.0.0.1:8000"},
        )
        assert submitted.status_code == 202
        await app.state.runtime.scheduler.join()
        result = await client.get(submitted.headers["Location"])
        assert result.json()["status"] == "SUCCEEDED"


async def test_workspace_allows_link_navigation_but_keeps_api_origin_checks(assets: Path) -> None:
    app = create_workspace(Settings(), assets)
    navigation = {
        "Sec-Fetch-Site": "cross-site",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Dest": "document",
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000"
    ) as client:
        assert (await client.get("/", headers=navigation)).status_code == 200
        assert (await client.get("/v1/workspace", headers=navigation)).status_code == 403
        assert (await client.post("/v1/runs", json={}, headers=navigation)).status_code == 403


@pytest.mark.parametrize(
    "headers",
    [
        {"Origin": "https://attacker.invalid"},
        {"Origin": "null"},
        {"Sec-Fetch-Site": "cross-site"},
        {"Sec-Fetch-Site": "same-site"},
    ],
)
async def test_workspace_rejects_cross_origin_browser_requests(
    assets: Path, headers: dict[str, str]
) -> None:
    app = create_workspace(Settings(), assets)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000"
    ) as client:
        response = await client.post("/v1/runs", json={}, headers=headers)
        assert response.status_code == 403
        assert (await client.get("/", headers={"Host": "attacker.invalid"})).status_code == 400


def test_workspace_rejects_missing_build_or_nonlocal_auth(assets: Path, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Build the workspace"):
        create_workspace(Settings(), tmp_path / "missing")
    with pytest.raises(ValueError, match="requires RUNWEAVE_AUTH_MODE=local"):
        create_workspace(
            Settings(
                auth_mode="jwt",
                auth_issuer="issuer",
                auth_audience="audience",
                auth_signing_key=SecretStr("s" * 32),
            ),
            assets,
        )
