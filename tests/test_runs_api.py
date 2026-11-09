import asyncio
from uuid import uuid4

import httpx
import pytest

from runweave.agents.contracts import IncidentRequest, IncidentResult
from runweave.app import create_app
from runweave.core.settings import Settings


@pytest.mark.asyncio
async def test_submit_poll_and_repeated_cancel_of_completed_run() -> None:
    app = create_app(Settings(worker_count=2))
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        response = await client.post(
            "/v1/runs", json={"service": "payments", "description": "Latency"}
        )
        assert response.status_code == 202
        submitted = response.json()
        assert submitted["status"] == "QUEUED"
        assert response.headers["Location"] == submitted["status_url"]
        async with asyncio.timeout(2):
            while True:
                response = await client.get(submitted["status_url"])
                if response.json()["status"] == "SUCCEEDED":
                    break
                await asyncio.sleep(0.01)
        completed = response.json()
        assert completed["result"]["evidence_ids"] == [
            "mock-health:payments",
            "mock-runbook:triage-v1",
        ]
        assert completed["started_at"] is not None
        assert completed["finished_at"] is not None
        assert (await client.post(f"{submitted['status_url']}/cancel")).json() == completed
        assert (await client.get("/health/ready")).status_code == 200


@pytest.mark.asyncio
async def test_unavailable_not_found_and_invalid_input_responses() -> None:
    app = create_app(Settings())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/runs", json={"service": "payments", "description": "Latency"}
        )
        assert response.status_code == 503
        assert response.headers["Retry-After"] == "1"
        async with app.router.lifespan_context(app):
            assert (await client.get(f"/v1/runs/{uuid4()}")).status_code == 404
            assert (await client.post(f"/v1/runs/{uuid4()}/cancel")).status_code == 404
            assert (await client.get("/v1/runs/not-a-uuid")).status_code == 422
            assert (await client.post("/v1/runs", json={"service": "../secret"})).status_code == 422


class BlockingExecutor:
    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def run(self, incident: IncidentRequest) -> IncidentResult:
        self.started.set()
        await asyncio.Event().wait()
        raise AssertionError("Unreachable")


@pytest.mark.asyncio
async def test_saturation_and_cancellation_api_contracts() -> None:
    executor = BlockingExecutor()
    app = create_app(Settings(worker_count=1, queue_capacity=1), executor=executor)
    payload = {"service": "payments", "description": "Latency"}
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        running = (await client.post("/v1/runs", json=payload)).json()
        await asyncio.wait_for(executor.started.wait(), 1)
        queued = (await client.post("/v1/runs", json=payload)).json()
        rejected = await client.post("/v1/runs", json=payload)
        assert rejected.status_code == 503
        assert rejected.headers["Retry-After"] == "1"
        assert rejected.json()["detail"]["code"] == "QUEUE_FULL"
        assert (await client.get("/health/ready")).status_code == 200
        cancelled = await client.post(f"{queued['status_url']}/cancel")
        assert cancelled.status_code == 200
        assert cancelled.json()["status"] == "CANCELLED"
        assert (await client.post(f"{queued['status_url']}/cancel")).json() == cancelled.json()
        assert (await client.post(f"{running['status_url']}/cancel")).json()[
            "status"
        ] == "CANCELLED"
