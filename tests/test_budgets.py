import asyncio
import json
from decimal import Decimal
from typing import cast

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from runweave.agents.budgeted import BudgetedModel
from runweave.agents.context import SYSTEM_PROMPT, compact_context, estimated_tokens, model_payload
from runweave.agents.contracts import (
    Evidence,
    IncidentRequest,
    ModelDecision,
    ToolCall,
    ToolExchange,
)
from runweave.agents.fake import FakeAgentModel
from runweave.agents.groq import GroqAgentModel
from runweave.agents.runner import AgentRunner
from runweave.app import create_app
from runweave.budgets import (
    BudgetExceeded,
    BudgetPolicy,
    BudgetState,
    MemoryBudget,
    Reservation,
    budget_scope,
)
from runweave.core.runtime import Runtime
from runweave.core.settings import Settings
from runweave.telemetry import Telemetry
from runweave.tools.contracts import HealthObservation, HealthQuery
from runweave.tools.registry import HEALTH, RUNBOOK, ToolRegistry

INCIDENT = IncidentRequest(service="payments", description="Synthetic latency")


class Health:
    async def execute(self, query: HealthQuery) -> HealthObservation:
        return HealthObservation(service=query.service, status="healthy", evidence_id="health:1")


def history() -> tuple[ToolExchange, ...]:
    return (
        ToolExchange(
            ToolCall(id="health-call", name=HEALTH, arguments='{"service":"payments"}'),
            Evidence(
                "health:1", '{"service":"payments","status":"healthy","evidence_id":"health:1"}'
            ),
        ),
        ToolExchange(
            ToolCall(id="runbook-call", name=RUNBOOK, arguments='{"service":"payments"}'),
            Evidence("runbook:1", "\u03bb" * 4000),
        ),
    )


async def test_parallel_reservations_cannot_overspend() -> None:
    policy = BudgetPolicy(
        max_requests=4,
        max_input_tokens=30,
        max_output_tokens=30,
        input_nanos_per_token=1,
        output_nanos_per_token=2,
        max_cost_nanos=60,
    )
    ledger = MemoryBudget(BudgetState(policy=policy))
    results = await asyncio.gather(
        *(Reservation.reserve(ledger, "fake-v1", 10, 10) for _ in range(20)), return_exceptions=True
    )
    assert sum(isinstance(result, Reservation) for result in results) == 2
    assert all(isinstance(result, (Reservation, BudgetExceeded)) for result in results)
    assert ledger.state.requests == 2
    assert ledger.state.input_tokens == ledger.state.output_tokens == 20
    assert ledger.state.summary().estimated_cost_usd == Decimal("0.000000060")


async def test_settlement_is_idempotent_and_releases_only_known_usage() -> None:
    ledger = MemoryBudget()
    reservation = await Reservation.reserve(ledger, "fake-v1", 100, 50)
    await reservation.settle(10, 5)
    await reservation.settle(1, 1)
    assert (ledger.state.input_tokens, ledger.state.output_tokens) == (10, 5)
    assert ledger.state.summary().uncertain_requests == 0
    another = await Reservation.reserve(ledger, "fake-v1", 100, 50)
    await another.settle(None, 5)
    assert (ledger.state.input_tokens, ledger.state.output_tokens) == (110, 10)
    assert ledger.state.summary().uncertain_requests == 1


async def test_provider_overage_is_recorded_before_failure() -> None:
    ledger = MemoryBudget(BudgetState(policy=BudgetPolicy(max_output_tokens=100)))
    reservation = await Reservation.reserve(ledger, "fake-v1", 10, 5)
    with pytest.raises(BudgetExceeded):
        await reservation.settle(10, 8)
    assert ledger.state.output_tokens == 8
    assert ledger.state.blocked
    with pytest.raises(BudgetExceeded):
        await Reservation.reserve(ledger, "fake-v1", 10, 1)
    assert ledger.state.requests == 1


class CountingModel(FakeAgentModel):
    def __init__(self) -> None:
        self.calls = 0

    async def decide(
        self,
        incident: IncidentRequest,
        history: tuple[ToolExchange, ...],
        tools: tuple[dict[str, object], ...],
        completion_budget: int,
    ) -> ModelDecision:
        self.calls += 1
        return await super().decide(incident, history, tools, completion_budget)


@pytest.mark.parametrize(
    "updates",
    [
        {"context_tokens": 1},
        {"max_input_tokens": 1},
        {"input_nanos_per_token": 1000, "max_cost_nanos": 1},
    ],
)
async def test_preflight_denies_before_model_call(updates: dict[str, int]) -> None:
    policy = BudgetPolicy.model_validate(updates)
    ledger = MemoryBudget(BudgetState(policy=policy))
    model = CountingModel()
    bounded = BudgetedModel(model, policy, Telemetry())
    with budget_scope(ledger), pytest.raises(BudgetExceeded):
        await bounded.decide(INCIDENT, (), ToolRegistry(Health()).definitions(), 512)
    assert model.calls == ledger.state.requests == 0


def test_context_compaction_preserves_instructions_evidence_and_pairs() -> None:
    original = history()
    tools = ToolRegistry(Health()).definitions()
    policy = BudgetPolicy(evidence_bytes=128)
    prepared, count, changed = compact_context(INCIDENT, original, tools, policy, 512)
    assert changed
    assert prepared[0] == original[0]
    assert len(prepared[1].evidence.content.encode()) <= 128
    assert prepared[1].evidence.id == original[1].evidence.id
    assert prepared[1].call == original[1].call
    assert original[1].evidence.content == "\u03bb" * 4000
    payload = model_payload(INCIDENT, prepared, tools, policy.model, 512)
    messages = cast(list[dict[str, object]], payload["messages"])
    assert messages[0]["content"] == SYSTEM_PROMPT
    assert messages[1]["content"] == INCIDENT.model_dump_json()
    assert [item["tool_call_id"] for item in messages if item["role"] == "tool"] == [
        "health-call",
        "runbook-call",
    ]
    assert count == estimated_tokens(payload)
    assert count > len(json.dumps(payload).encode())
    assert (prepared, count, changed) == compact_context(INCIDENT, original, tools, policy, 512)


def test_context_compacts_to_reference_before_rejecting() -> None:
    tools = ToolRegistry(Health()).definitions()
    original = history()
    minimal = (original[0], ToolExchange(original[1].call, Evidence("runbook:1", "[compacted]")))
    minimum = estimated_tokens(model_payload(INCIDENT, minimal, tools, "fake-v1", 512)) + 512
    prepared, count, _ = compact_context(
        INCIDENT, original, tools, BudgetPolicy(context_tokens=minimum), 512
    )
    assert count + 512 == minimum
    assert prepared == minimal
    with pytest.raises(BudgetExceeded):
        compact_context(INCIDENT, original, tools, BudgetPolicy(context_tokens=minimum - 1), 512)


class WaitingModel(CountingModel):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()

    async def decide(
        self,
        incident: IncidentRequest,
        history: tuple[ToolExchange, ...],
        tools: tuple[dict[str, object], ...],
        completion_budget: int,
    ) -> ModelDecision:
        self.started.set()
        await asyncio.Event().wait()
        raise AssertionError("Unreachable")


async def test_cancellation_preserves_charge_and_propagates() -> None:
    model = WaitingModel()
    policy = BudgetPolicy()
    ledger = MemoryBudget(BudgetState(policy=policy))
    with budget_scope(ledger):
        task = asyncio.create_task(AgentRunner(model, Health(), budget_policy=policy).run(INCIDENT))
        await asyncio.wait_for(model.started.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert ledger.state.requests == 1
    assert ledger.state.input_tokens > 0
    assert ledger.state.output_tokens == 512
    assert ledger.state.summary().uncertain_requests == 1


@pytest.mark.parametrize("workflow", ["baseline", "pydanticai"])
@pytest.mark.parametrize(
    "limits,code,requests",
    [
        ({"agent_context_tokens": 1}, "CONTEXT_LIMIT_EXCEEDED", 0),
        ({"agent_input_tokens": 1}, "RUN_BUDGET_EXCEEDED", 0),
        ({"agent_request_limit": 1}, "RUN_BUDGET_EXCEEDED", 1),
    ],
)
async def test_budget_failure_and_usage_through_api(
    workflow: str, limits: dict[str, int], code: str, requests: int
) -> None:
    if workflow == "pydanticai":
        pytest.importorskip("pydantic_ai")
    settings = Settings.model_validate({"agent_workflow": workflow, "worker_count": 1, **limits})
    app = create_app(settings)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        admitted = await client.post("/v1/runs", json=INCIDENT.model_dump())
        assert admitted.status_code == 202
        runtime = cast(Runtime, app.state.runtime)
        await asyncio.wait_for(runtime.scheduler.join(), 2)
        response = await client.get(admitted.headers["Location"])
        result = response.json()
        assert result["status"] == "FAILED"
        assert result["error"]["code"] == code
        assert result["usage"]["requests"] == requests
        assert Decimal(result["usage"]["estimated_cost_usd"]) == 0
        assert 'reason="denied"' in await runtime.scheduler.metrics()


@pytest.mark.parametrize(
    "usage", [None, {"completion_tokens": 3}, {"prompt_tokens": 7, "completion_tokens": 3}]
)
async def test_groq_usage_is_preserved_when_action_validation_fails(
    usage: dict[str, int] | None,
) -> None:
    policy = BudgetPolicy(
        model="llama-3.3-70b-versatile",
        price_version="mock-v1",
        input_nanos_per_token=590,
        output_nanos_per_token=790,
    )
    ledger = MemoryBudget(BudgetState(policy=policy))

    async def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [], "usage": usage})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        model = GroqAgentModel(
            client,
            SecretStr("synthetic-key"),
            model=policy.model,
            concurrency=1,
            requests_per_minute=600,
        )
        with budget_scope(ledger), pytest.raises(ValueError):
            await AgentRunner(model, Health(), budget_policy=policy).run(INCIDENT)
    assert ledger.state.requests == 1
    assert ledger.state.output_tokens == (usage["completion_tokens"] if usage else 512)
    if usage and "prompt_tokens" in usage:
        assert ledger.state.input_tokens == 7
        assert ledger.state.summary().uncertain_requests == 0
    else:
        assert ledger.state.input_tokens > 1000
        assert ledger.state.summary().uncertain_requests == 1


async def test_transient_failures_and_retry_attempts_share_the_same_ledger() -> None:
    policy = BudgetPolicy(model="llama-3.3-70b-versatile", max_requests=2)
    ledger = MemoryBudget(BudgetState(policy=policy))
    calls = 0

    async def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("synthetic lost response", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        model = GroqAgentModel(
            client,
            SecretStr("synthetic-key"),
            model=policy.model,
            concurrency=1,
            requests_per_minute=600,
        )
        runner = AgentRunner(model, Health(), budget_policy=policy)
        with budget_scope(ledger):
            for _ in range(2):
                with pytest.raises(httpx.ReadTimeout):
                    await runner.run(INCIDENT)
            with pytest.raises(BudgetExceeded):
                await runner.run(INCIDENT)
    assert calls == ledger.state.requests == 2
    assert ledger.state.output_tokens == 1024
    assert ledger.state.summary().uncertain_requests == 2


def test_live_pricing_requires_an_explicit_version_and_both_rates() -> None:
    with pytest.raises(ValidationError, match="versioned input and output prices"):
        Settings(model_provider="groq", groq_api_key=SecretStr("synthetic-key"))
    settings = Settings(
        model_provider="groq",
        groq_api_key=SecretStr("synthetic-key"),
        model_input_usd_per_million=Decimal("0.59"),
        model_output_usd_per_million=Decimal("0.79"),
        model_price_version="mock-v1",
    )
    assert settings.budget_policy().input_nanos_per_token == 590
    assert settings.budget_policy().output_nanos_per_token == 790


async def test_parallel_model_calls_share_reservations() -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    class Provider(CountingModel):
        async def decide(
            self,
            incident: IncidentRequest,
            history: tuple[ToolExchange, ...],
            tools: tuple[dict[str, object], ...],
            completion_budget: int,
        ) -> ModelDecision:
            nonlocal calls
            calls += 1
            if calls == 2:
                started.set()
            await release.wait()
            return ModelDecision(
                ToolCall(id="fixture", name=HEALTH, arguments='{"service":"payments"}'),
                1,
                input_tokens=1,
            )

    policy = BudgetPolicy(max_output_tokens=1024)
    ledger = MemoryBudget(BudgetState(policy=policy))
    model = BudgetedModel(Provider(), policy, Telemetry())
    with budget_scope(ledger):
        tasks = [asyncio.create_task(model.decide(INCIDENT, (), (), 512)) for _ in range(20)]
        try:
            await asyncio.wait_for(started.wait(), 1)
            await asyncio.sleep(0)
            assert calls == 2
        finally:
            release.set()
            results = await asyncio.gather(*tasks, return_exceptions=True)
    assert sum(isinstance(result, ModelDecision) for result in results) == 2
    assert sum(isinstance(result, BudgetExceeded) for result in results) == 18
    assert ledger.state.requests == 2
    assert ledger.state.input_tokens == ledger.state.output_tokens == 2


@pytest.mark.parametrize(
    "raw", [{}, {"policy": {"version": "budget-v0"}}, {"policy": {}, "input_tokens": -1}]
)
def test_invalid_persisted_budgets_fail_closed(raw: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        BudgetState.model_validate(raw)


@pytest.mark.parametrize("workflow", ["baseline", "pydanticai"])
@pytest.mark.parametrize(
    "limits",
    [{"agent_context_tokens": 1}, {"agent_input_tokens": 1}, {"agent_max_cost_usd": "0.000000001"}],
)
async def test_live_adapter_preflight_never_opens_transport(
    workflow: str,
    limits: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if workflow == "pydanticai":
        pytest.importorskip("pydantic_ai")

    async def unexpected(self: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        pytest.fail("A denied reservation reached the provider transport")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", unexpected)
    app = create_app(
        Settings.model_validate(
            {
                "model_provider": "groq",
                "groq_api_key": "synthetic-key",
                "model_input_usd_per_million": "0.59",
                "model_output_usd_per_million": "0.79",
                "model_price_version": "mock-v1",
                "agent_workflow": workflow,
                "worker_count": 1,
                **limits,
            }
        )
    )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        submitted = await client.post("/v1/runs", json=INCIDENT.model_dump())
        runtime = cast(Runtime, app.state.runtime)
        await asyncio.wait_for(runtime.scheduler.join(), 2)
        completed = (await client.get(submitted.headers["Location"])).json()
        assert completed["status"] == "FAILED"
        assert completed["usage"]["requests"] == 0
        assert completed["error"]["code"] in {"CONTEXT_LIMIT_EXCEEDED", "RUN_BUDGET_EXCEEDED"}
