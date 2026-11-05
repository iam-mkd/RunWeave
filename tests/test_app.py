from typing import cast

import httpx
import pytest

from runweave.app import create_app
from runweave.core.runtime import Runtime
from runweave.core.settings import Settings


@pytest.mark.asyncio
async def test_readiness_tracks_lifespan_and_client_is_closed_on_shutdown() -> None:
    app = create_app(Settings())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.get("/health/live")).json() == {"status": "alive"}
        assert (await client.get("/health/ready")).status_code == 503

        async with app.router.lifespan_context(app):
            runtime = cast(Runtime, app.state.runtime)
            response = await client.get("/health/ready")
            assert response.status_code == 200
            assert response.json() == {"status": "ready"}
            assert not runtime.client.is_closed

        assert runtime.client.is_closed
        assert app.state.runtime is None
        assert (await client.get("/health/ready")).status_code == 503


@pytest.mark.asyncio
async def test_client_is_closed_when_lifespan_exits_with_an_error() -> None:
    app = create_app(Settings())
    with pytest.raises(RuntimeError, match="test failure"):
        async with app.router.lifespan_context(app):
            runtime = cast(Runtime, app.state.runtime)
            raise RuntimeError("test failure")

    assert runtime.client.is_closed
    assert app.state.runtime is None
