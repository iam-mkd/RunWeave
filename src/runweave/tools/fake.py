import asyncio

import httpx

from runweave.tools.contracts import HealthObservation, HealthQuery


async def fake_health_response(request: httpx.Request) -> httpx.Response:
    await asyncio.sleep(0.01)
    if (
        request.method != "GET"
        or request.url.host != "mock-tools.invalid"
        or not request.url.path.startswith("/health/")
    ):
        return httpx.Response(404)

    query = HealthQuery(service=request.url.path.removeprefix("/health/"))
    observation = HealthObservation(
        service=query.service, status="degraded", evidence_id=f"mock-health:{query.service}"
    )
    return httpx.Response(200, json=observation.model_dump())


class FakeHealthTool:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def execute(self, query: HealthQuery) -> HealthObservation:
        body = bytearray()
        async with self._client.stream(
            "GET", f"https://mock-tools.invalid/health/{query.service}", follow_redirects=False
        ) as response:
            response.raise_for_status()
            async for chunk in response.aiter_bytes(chunk_size=4096):
                if len(body) + len(chunk) > 16384:
                    raise ValueError("Health response exceeds the size limit")
                body.extend(chunk)
        return HealthObservation.model_validate_json(body)
