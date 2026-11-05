import asyncio

import pytest
from pydantic import ValidationError

from runweave.agents.contracts import IncidentRequest, IncidentResult
from runweave.agents.fake import FakeAgentModel
from runweave.agents.runner import AgentRunner
from runweave.core.runtime import open_runtime
from runweave.core.settings import Settings
from runweave.demo import run_demo
from runweave.tools.contracts import HealthObservation, HealthQuery


@pytest.mark.asyncio
async def test_offline_run_returns_structured_result_with_evidence() -> None:
    incident = IncidentRequest(service="payments", description="Elevated latency")
    result = await run_demo(incident, Settings())
    assert result.severity == "medium"
    assert result.evidence_ids == ("mock-health:payments",)
    assert "Elevated latency" in result.summary
    assert "degraded" in result.summary
    assert IncidentResult.model_validate_json(result.model_dump_json()) == result


class HealthyTool:
    async def execute(self, query: HealthQuery) -> HealthObservation:
        return HealthObservation(
            service=query.service, status="healthy", evidence_id="replacement-tool:1"
        )


@pytest.mark.asyncio
async def test_runner_accepts_a_replacement_tool_without_inheritance() -> None:
    runner = AgentRunner(FakeAgentModel(), HealthyTool())
    result = await runner.run(IncidentRequest(service="payments", description="Check latency"))
    assert result.severity == "low"
    assert result.evidence_ids == ("replacement-tool:1",)


class MismatchedTool:
    async def execute(self, query: HealthQuery) -> HealthObservation:
        return HealthObservation(service="other", status="healthy", evidence_id="wrong:1")


@pytest.mark.asyncio
async def test_runner_rejects_observation_for_another_service() -> None:
    runner = AgentRunner(FakeAgentModel(), MismatchedTool())
    with pytest.raises(ValueError, match="does not match"):
        await runner.run(IncidentRequest(service="payments", description="Check latency"))


class WaitingTool:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cleaned_up = False

    async def execute(self, query: HealthQuery) -> HealthObservation:
        self.started.set()
        try:
            await asyncio.Event().wait()
        finally:
            self.cleaned_up = True
        raise AssertionError("Unreachable")


@pytest.mark.asyncio
async def test_runner_propagates_cancellation_to_tool() -> None:
    tool = WaitingTool()
    runner = AgentRunner(FakeAgentModel(), tool)
    task = asyncio.create_task(
        runner.run(IncidentRequest(service="payments", description="Check latency"))
    )
    await asyncio.wait_for(tool.started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert tool.cleaned_up


@pytest.mark.parametrize("service", ["../secrets", "https://example.com", "", "a" * 64])
def test_incident_rejects_invalid_service_names(service: str) -> None:
    with pytest.raises(ValidationError):
        IncidentRequest(service=service, description="Latency")


@pytest.mark.parametrize("description", [" ", "x" * 2001])
def test_incident_rejects_empty_or_oversized_description(description: str) -> None:
    with pytest.raises(ValidationError):
        IncidentRequest(service="payments", description=description)


@pytest.mark.asyncio
async def test_runtime_releases_client_after_tool_failure() -> None:
    async with open_runtime(Settings()) as runtime:
        await runtime.client.aclose()
        with pytest.raises(RuntimeError, match="client has been closed"):
            await runtime.runner.run(IncidentRequest(service="payments", description="Latency"))
    assert runtime.client.is_closed
