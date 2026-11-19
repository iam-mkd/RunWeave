"""The callback HTTP agents use to request approvals authenticates with the run's token."""

import httpx
import pytest

from runweave.app import create_app
from runweave.core.settings import Settings


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Bearer rwp_" + "x" * 40}, {"Authorization": "Basic abc"}],
)
async def test_approval_callbacks_need_a_live_run_token(headers: dict[str, str]) -> None:
    app = create_app(
        Settings(_env_file=None, model_provider="fake", key_store="none"),  # type: ignore[call-arg]
        agents=[],
    )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://rw") as client,
    ):
        response = await client.post(
            "/agent/v1/approvals",
            json={"action": "issue_refund", "arguments": {"order": "x"}},
            headers=headers,
        )
    assert response.status_code == 401
    assert response.json() == {"error": {"code": "APPROVAL_TOKEN_INVALID"}}
