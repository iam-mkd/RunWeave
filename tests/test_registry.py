from datetime import UTC, datetime, tzinfo

import pytest

from runweave.agents.contracts import IncidentRequest, IncidentResult
from runweave.orchestration.models import RunStatus
from runweave.orchestration.registry import RunRegistry
from runweave.security import LOCAL_PRINCIPAL


def incident() -> IncidentRequest:
    return IncidentRequest(service="payments", description="Latency")


def result() -> IncidentResult:
    return IncidentResult(summary="Synthetic result", severity="low", evidence_ids=("mock:1",))


def test_first_terminal_transition_wins() -> None:
    registry = RunRegistry(10, 60)
    run = registry.create(incident())
    assert registry.claim(run.id) == incident()
    cancelled = registry.finish(run.id, RunStatus.CANCELLED)
    assert registry.finish(run.id, RunStatus.SUCCEEDED, result=result()) == cancelled
    assert cancelled is not None and cancelled.result is None


def test_queued_run_cannot_succeed_without_starting() -> None:
    registry = RunRegistry(10, 60)
    run = registry.create(incident())
    with pytest.raises(ValueError, match="Queued"):
        registry.finish(run.id, RunStatus.SUCCEEDED, result=result())


def test_terminal_payloads_must_match_status() -> None:
    registry = RunRegistry(10, 60)
    run = registry.create(incident())
    registry.claim(run.id)
    with pytest.raises(ValueError, match="result"):
        registry.finish(run.id, RunStatus.SUCCEEDED)
    with pytest.raises(ValueError, match="error"):
        registry.finish(run.id, RunStatus.FAILED)
    assert registry.get(run.id) is not None


def test_history_cap_evicts_oldest_terminal_run_but_preserves_active_work() -> None:
    registry = RunRegistry(1, 60)
    active = registry.create(incident())
    registry.claim(active.id)
    first = registry.create(incident())
    second = registry.create(incident())
    registry.finish(first.id, RunStatus.CANCELLED)
    registry.finish(second.id, RunStatus.CANCELLED)
    assert registry.get(first.id) is None
    assert registry.get(second.id) is not None
    assert registry.get(active.id) is not None


def test_retention_uses_terminal_time_and_does_not_expire_running_work() -> None:
    now = [0.0]
    registry = RunRegistry(10, 5, clock=lambda: now[0])
    active = registry.create(incident())
    registry.claim(active.id)
    completed = registry.create(incident())
    now[0] = 100
    registry.finish(completed.id, RunStatus.CANCELLED)
    now[0] = 104.9
    assert registry.get(completed.id) is not None
    now[0] = 105
    assert registry.get(completed.id) is None
    assert registry.get(active.id) is not None


def test_claim_only_starts_a_queued_run_once() -> None:
    registry = RunRegistry(10, 60)
    run = registry.create(incident())
    assert registry.claim(run.id) is not None
    assert registry.claim(run.id) is None
    snapshot = registry.get(run.id)
    assert snapshot is not None and snapshot.started_at is not None
    assert run.status == RunStatus.QUEUED


def test_listing_keeps_admission_order_for_identical_timestamps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instant = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)

    class FrozenClock(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> "FrozenClock":
            return cls.fromtimestamp(instant.timestamp(), tz)

    monkeypatch.setattr("runweave.orchestration.registry.datetime", FrozenClock)
    registry = RunRegistry(10, 60)
    ids = [registry.create(incident()).id for _ in range(5)]
    listed = registry.list(LOCAL_PRINCIPAL)
    assert [run.id for run in listed] == list(reversed(ids))
    assert len({run.submitted_at for run in listed}) == 1


def test_unfinished_count_follows_each_tenants_runs_through_their_lifecycle() -> None:
    now = [0.0]
    registry = RunRegistry(1, 60, clock=lambda: now[0])
    other = LOCAL_PRINCIPAL.model_copy(update={"tenant": "other"})
    queued = registry.create(incident())
    running = registry.create(incident())
    registry.create(incident(), other)
    registry.claim(running.id)
    assert registry.unfinished_for_tenant("local") == 2
    assert registry.unfinished_for_tenant("other") == 1
    assert registry.unfinished_for_tenant("nobody") == 0

    registry.finish(running.id, RunStatus.SUCCEEDED, result=result())
    registry.finish(running.id, RunStatus.CANCELLED)  # a late result changes nothing
    assert registry.unfinished_for_tenant("local") == 1

    registry.finish(queued.id, RunStatus.CANCELLED)  # evicts `running` from history
    assert registry.get(running.id) is None
    assert registry.unfinished_for_tenant("local") == 0
    assert registry.unfinished_for_tenant("other") == 1

    registry.cancel_unfinished()
    now[0] = 120  # past the TTL, so every terminal run is pruned
    registry.prune()
    assert len(registry) == 0
    assert registry.unfinished_for_tenant("other") == 0
