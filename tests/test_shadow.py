import asyncio
import json
from contextvars import ContextVar

import pytest
from pydantic import ValidationError
from test_memory import fact

from runweave.agents.contracts import IncidentRequest
from runweave.approvals import current_ticket_gate
from runweave.budgets import BudgetPolicy, MemoryBudget, budget_scope, current_ledger
from runweave.checkpoints import current_checkpoints
from runweave.evaluation import shadow
from runweave.evaluation.shadow import (
    GATE_NAMES,
    ReleaseSelection,
    ShadowReplay,
    ShadowSnapshot,
    pin,
    promote,
    rollback,
)
from runweave.evaluation.suite import (
    CANARY,
    Fixture,
    Observation,
    Workflow,
    execute_fixture,
    load_fixtures,
)
from runweave.memory import MemorySelection
from runweave.security import LOCAL_PRINCIPAL


def snapshot() -> ShadowSnapshot:
    return ShadowSnapshot(
        fixture=Fixture(
            name="shadow", incident=IncidentRequest(service="payments", description="Synthetic")
        )
    )


async def test_same_input_fresh_contexts_independent_budgets_and_no_real_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker: ContextVar[str] = ContextVar("ambient_database_handle", default="absent")
    marker.set("must-not-leak")
    execute = execute_fixture
    ledgers = []
    incident = snapshot().fixture.incident.model_copy(
        update={"memory": MemorySelection(), "create_ticket": True}
    )
    item = ShadowSnapshot(
        fixture=snapshot().fixture.model_copy(update={"incident": incident}), facts=(fact(),)
    )
    seen = []

    async def observe(
        fixture: Fixture, workflow: Workflow, *, tool_delay: float = 0
    ) -> Observation:
        assert marker.get() == "absent" and current_checkpoints() is None
        assert type(current_ticket_gate()).__name__ == "SimulatedTicket"
        ledgers.append(current_ledger())
        seen.append(fixture)
        return await execute(fixture, workflow, tool_delay=tool_delay)

    monkeypatch.setattr(shadow, "execute_fixture", observe)
    ambient = MemoryBudget()
    with budget_scope(ambient):
        async with ShadowReplay(pin("baseline"), pin("coordinated")) as replay:
            pair = await replay.compare(item)
    assert pair.user_result == pair.primary.result == pair.candidate.result
    assert pair.primary.simulated_effects == pair.candidate.simulated_effects == 1
    assert all(pair.candidate.quality.values()) and seen[0] == seen[1]
    assert ledgers[0] is not ledgers[1] and ambient.state.requests == 0
    assert pair.primary.usage.requests == pair.candidate.usage.requests == 3
    assert item.facts[0].content == "Historical data" and replay.queue.empty()


@pytest.mark.parametrize("change", ["foreign", "expired", "secret", "scope", "project"])
def test_snapshot_authorization_and_sanitization(change: str) -> None:
    base = snapshot().fixture
    if change in {"foreign", "expired", "project"}:
        incident = base.incident.model_copy(update={"memory": MemorySelection()})
        item = fact(expired=change == "expired")
        if change == "foreign":
            item = item.model_copy(update={"tenant": "foreign"})
        if change == "project":
            item = item.model_copy(update={"project": "shared"})
        with pytest.raises(ValidationError):
            ShadowSnapshot(fixture=base.model_copy(update={"incident": incident}), facts=(item,))
    else:
        if change == "secret":
            base = base.model_copy(update={"runbook": CANARY, "protected_values": (CANARY,)})
        else:
            base = base.model_copy(
                update={"principal": LOCAL_PRINCIPAL.model_copy(update={"scopes": frozenset()})}
            )
        with pytest.raises(ValidationError) as error:
            ShadowSnapshot(fixture=base)
        assert CANARY not in str(error.value)


async def test_candidate_budget_exhaustion_does_not_change_primary() -> None:
    async with ShadowReplay(
        pin("baseline"), pin("coordinated"), candidate_budget=BudgetPolicy(max_input_tokens=1)
    ) as replay:
        pair = await replay.compare(snapshot())
    assert pair.user_result is not None and pair.primary.outcome == "succeeded"
    assert pair.candidate.outcome == "rejected" and pair.candidate.usage.requests == 0


async def test_candidate_queue_saturation_timeout_and_shutdown_are_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execute = execute_fixture
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def hang(fixture: Fixture, workflow: Workflow, **kwargs: object) -> Observation:
        if workflow == "coordinated":
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        return await execute(fixture, workflow)

    monkeypatch.setattr(shadow, "execute_fixture", hang)
    async with ShadowReplay(
        pin("baseline"), pin("coordinated"), queue_capacity=1, timeout=0.3
    ) as replay:
        first = asyncio.create_task(replay.compare(snapshot()))
        await entered.wait()
        second = asyncio.create_task(replay.compare(snapshot()))
        async with asyncio.timeout(1):
            while replay.queue.empty():  # noqa: ASYNC110
                await asyncio.sleep(0.01)
        third = await replay.compare(snapshot())
        assert third.primary.result is not None and third.candidate.outcome == "unavailable"
        a, b = await asyncio.gather(first, second)
        assert a.candidate.outcome == "timeout" and b.candidate.outcome in {
            "timeout",
            "unavailable",
        }
    assert cancelled.is_set() and replay.worker is not None and replay.worker.done()
    assert not any(t.get_name() == "shadow-candidate" for t in asyncio.all_tasks())


async def test_cancelling_comparison_cancels_active_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execute = execute_fixture
    entered, stopped = asyncio.Event(), asyncio.Event()

    async def hang(fixture: Fixture, workflow: Workflow, **kwargs: object) -> Observation:
        if workflow == "coordinated":
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
        return await execute(fixture, workflow)

    monkeypatch.setattr(shadow, "execute_fixture", hang)
    async with ShadowReplay(pin("baseline"), pin("coordinated")) as replay:
        task = asyncio.create_task(replay.compare(snapshot()))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(stopped.wait(), 1)
        await replay.queue.join()


def test_promotion_and_rollback_require_pins_and_passing_gates() -> None:
    before, after = pin("baseline"), pin("coordinated")
    selection = ReleaseSelection(active=before)
    report: dict[str, object] = {
        "primary_pin": before.model_dump(),
        "candidate_pin": after.model_dump(),
        "policy": "offline-shadow-v1",
        "promotion_eligible": True,
        "gates": dict.fromkeys(GATE_NAMES, True),
        "fixture_sha256": load_fixtures()[1],
    }
    promoted = promote(selection, report)
    assert promoted.active == after and rollback(promoted).active == before
    for update in (
        {"promotion_eligible": False},
        {"gates": {"quality": False}},
        {"candidate_pin": {"workflow": "coordinated", "sha256": "0" * 64}},
    ):
        with pytest.raises(ValueError):
            promote(selection, {**report, **update})


async def test_stale_pin_fails_before_execution() -> None:
    with pytest.raises(ValueError, match="PIN_MISMATCH"):
        async with ShadowReplay(
            pin("baseline").model_copy(update={"sha256": "0" * 64}), pin("coordinated")
        ):
            raise AssertionError("Must not start")


async def test_shutdown_awaits_active_primary_and_rejects_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered, stopped = asyncio.Event(), asyncio.Event()

    async def block(fixture: Fixture, workflow: Workflow, **kwargs: object) -> Observation:
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()
        raise AssertionError("unreachable")

    monkeypatch.setattr(shadow, "execute_fixture", block)
    replay = ShadowReplay(pin("baseline"), pin("coordinated"))
    async with replay:
        task = asyncio.create_task(replay.compare(snapshot()))
        await entered.wait()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stopped.is_set() and not replay.primary_tasks and not replay.pending
    with pytest.raises(ValueError):
        async with replay:
            raise AssertionError("Must not restart")


async def test_real_shadow_report_blocks_cost_regressions_and_omits_credentials() -> None:
    result = await shadow.report()
    assert result["promotion_eligible"] is False
    gates = result["gates"]
    assert isinstance(gates, dict) and gates["full_evaluation_passed"] is True
    assert gates["candidate_estimated_cost_at_most_105_percent"] is False
    assert result["excluded_unsanitized_cases"] == ["incident-credential", "retrieved-credential"]
    assert CANARY not in json.dumps(result)
