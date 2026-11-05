import httpx

from runweave.tools.contracts import HealthObservation, HealthQuery


def fake_health_response(request: httpx.Request) -> httpx.Response:
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
        response = await self._client.get(f"/health/{query.service}")
        response.raise_for_status()
        return HealthObservation.model_validate(response.json())
