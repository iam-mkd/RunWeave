import asyncio
import json
from typing import cast

import httpx
import pytest

from runweave.agents.contracts import IncidentRequest, IncidentResult
from runweave.app import create_app
from runweave.core.runtime import Runtime
from runweave.core.settings import Settings


class FailingAgent:
    async def run(self, incident: IncidentRequest) -> IncidentResult:
        raise ValueError(incident.description)


@pytest.mark.asyncio
async def test_metrics_logs_and_traces_omit_sensitive_fields(
    capsys: pytest.CaptureFixture[str],
) -> None:
    app = create_app(
        Settings(model_provider="fake", json_logs=True, console_traces=True),
        executor=FailingAgent(),
    )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        response = await client.post(
            "/v1/runs",
            json={"service": "private-service", "description": "secret-incident"},
            headers={"Authorization": "Bearer private-credential"},
        )
        assert response.status_code == 202
        runtime = cast(Runtime, app.state.runtime)
        await asyncio.wait_for(runtime.scheduler.join(), 1)
        metrics = await client.get("/metrics")
        assert metrics.status_code == 200
        assert 'runweave_runs_finished_total{status="FAILED"} 1' in metrics.text
        assert 'runweave_run_errors_total{category="validation"} 1' in metrics.text
        assert "runweave_active_runs 0" in metrics.text
        assert "runweave_queue_wait_seconds_count 1" in metrics.text
    captured = capsys.readouterr()
    event = json.loads(captured.err.strip())
    assert event["event"] == "run.finished"
    assert event["run_id"] == response.json()["id"]
    assert '"name": "agent.run"' in captured.out
    assert '"status_code": "ERROR"' in captured.out
    assert response.json()["id"] in captured.out
    observed = captured.err + captured.out + metrics.text
    assert all(
        secret not in observed
        for secret in (
            "secret-incident",
            "private-service",
            "private-credential",
        )
    )
