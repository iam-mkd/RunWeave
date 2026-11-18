"""Opt-in, offline shadow replay. No live providers, database handles or effect adapters."""

import argparse
import asyncio
import hashlib
import json
import platform
from contextvars import Context
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from runweave.agents.contracts import IncidentRequest, IncidentResult
from runweave.approvals import ticket_scope
from runweave.budgets import BudgetPolicy, BudgetState, BudgetSummary, MemoryBudget, budget_scope
from runweave.evaluation.suite import (
    Fixture,
    Workflow,
    check_observation,
    evaluate,
    execute_fixture,
    load_fixtures,
    source_hashes,
)
from runweave.memory import MemoryFact, MemorySelection, memory_source_scope
from runweave.security import ContentPolicy, PolicyDenied, Principal

READ_SCOPES = frozenset(
    {
        "runs:submit",
        "runs:read",
        "tools:check_health",
        "tools:lookup_runbook",
        "agents:delegate",
        "memory:read",
    }
)
GATE_NAMES = frozenset(
    {
        "full_evaluation_passed",
        "all_paired_quality_passed",
        "outcome_and_evidence_parity",
        "candidate_estimated_cost_at_most_105_percent",
        "candidate_latency_at_most_2x_plus_50ms",
    }
)


class WorkflowPin(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    workflow: Workflow
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


def pin(workflow: Workflow) -> WorkflowPin:
    sources = source_hashes(workflow)
    sources["shadow_policy"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    return WorkflowPin(
        workflow=workflow,
        sha256=hashlib.sha256(json.dumps(sources, sort_keys=True).encode()).hexdigest(),
    )


class ShadowSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)
    fixture: Fixture = Field(repr=False)
    facts: tuple[MemoryFact, ...] = Field(default=(), max_length=5)

    @model_validator(mode="after")
    def authorized_and_sanitized(self) -> "ShadowSnapshot":
        fixture, now = self.fixture, datetime.now(UTC)
        fixture.principal.require("runs:submit")
        if fixture.incident.create_ticket:
            fixture.principal.require("tools:create_ticket")
        policy = ContentPolicy(fixture.protected_values)
        policy.check(fixture.incident.model_dump_json())
        policy.check(fixture.runbook)
        if fixture.incident.memory is not None:
            fixture.principal.require("memory:read")
            if fixture.incident.memory.project is not None:
                raise PolicyDenied()
        elif self.facts:
            raise PolicyDenied()
        if len(json.dumps([f.model_dump(mode="json") for f in self.facts]).encode()) > 4096:
            raise ValueError("SHADOW_SNAPSHOT_LIMIT")
        for fact in self.facts:
            if (
                fact.tenant != fixture.principal.tenant
                or fact.owner_subject != fixture.principal.subject
                or fact.project is not None
                or fact.service != fixture.incident.service
                or fact.expires_at <= now
            ):
                raise PolicyDenied()
            policy.check(fact.model_dump_json())
        return self

    def digest(self) -> str:
        return hashlib.sha256(self.model_dump_json().encode()).hexdigest()


@dataclass
class SnapshotReader:
    snapshot: ShadowSnapshot
    recall_bytes: int = 4096

    async def recall(
        self, service: str, selection: MemorySelection, principal: Principal
    ) -> tuple[MemoryFact, ...]:
        principal.require("memory:read")
        owner = self.snapshot.fixture.principal
        if (
            not principal.owns(owner)
            or service != self.snapshot.fixture.incident.service
            or selection.project
        ):
            raise PolicyDenied()
        return tuple(f for f in self.snapshot.facts if f.expires_at > datetime.now(UTC))


class Measurement(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    outcome: Literal["succeeded", "rejected", "timeout", "unavailable"]
    result: IncidentResult | None = None
    quality: dict[str, bool] = Field(default_factory=dict)
    usage: BudgetSummary
    elapsed_seconds: float
    queue_wait_seconds: float = 0
    simulated_effects: int = 0


@dataclass
class SimulatedTicket:
    calls: int = 0

    async def run(self, incident: IncidentRequest, result: IncidentResult) -> None:
        if self.calls or not incident.create_ticket:
            raise ValueError("SHADOW_EFFECT_INVALID")
        self.calls += 1


class Pair(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    case: str
    snapshot_sha256: str
    primary: Measurement
    candidate: Measurement

    @property
    def user_result(self) -> IncidentResult | None:
        return self.primary.result

    def differences(self) -> dict[str, object]:
        before, after = self.primary, self.candidate
        old = set(before.result.evidence_ids) if before.result else set()
        new = set(after.result.evidence_ids) if after.result else set()
        return {
            "outcome_equal": before.outcome == after.outcome,
            "evidence_added": sorted(new - old),
            "evidence_removed": sorted(old - new),
            "latency_seconds": after.elapsed_seconds - before.elapsed_seconds,
            "input_tokens": after.usage.input_tokens - before.usage.input_tokens,
            "output_tokens": after.usage.output_tokens - before.usage.output_tokens,
            "estimated_cost_usd": str(
                after.usage.estimated_cost_usd - before.usage.estimated_cost_usd
            ),
            "quality_regressions": sorted(
                k for k, v in before.quality.items() if v and not after.quality.get(k)
            ),
        }


@dataclass
class Work:
    snapshot: ShadowSnapshot
    expires: float
    future: asyncio.Future[Measurement]


class ShadowReplay:
    """Separate bounded candidate queue; fresh contexts prevent ambient durable-state access."""

    def __init__(
        self,
        primary: WorkflowPin,
        candidate: WorkflowPin,
        *,
        budget: BudgetPolicy | None = None,
        candidate_budget: BudgetPolicy | None = None,
        queue_capacity: int = 2,
        timeout: float = 5,
    ) -> None:
        if not 1 <= queue_capacity <= 16 or not 0 < timeout <= 30:
            raise ValueError("Invalid shadow limits")
        self.primary, self.candidate = primary, candidate
        self.budget = budget or BudgetPolicy(
            price_version="synthetic-shadow-v1",
            input_nanos_per_token=1000,
            output_nanos_per_token=2000,
        )
        self.candidate_budget = candidate_budget or self.budget.model_copy(deep=True)
        self.queue: asyncio.Queue[Work] = asyncio.Queue(queue_capacity)
        self.timeout = timeout
        self.worker: asyncio.Task[None] | None = None
        self.primary_tasks: set[asyncio.Task[Measurement]] = set()
        self.pending: set[asyncio.Future[Measurement]] = set()
        self.closed = False

    async def __aenter__(self) -> "ShadowReplay":
        if self.worker is not None or self.closed:
            raise ValueError("SHADOW_ALREADY_STARTED")
        for expected in (self.primary, self.candidate):
            if expected != await asyncio.to_thread(pin, expected.workflow):
                raise ValueError("SHADOW_PIN_MISMATCH")
        self.worker = asyncio.create_task(
            self._worker(), context=Context(), name="shadow-candidate"
        )
        return self

    async def __aexit__(self, *args: object) -> None:
        self.closed = True
        for task in tuple(self.primary_tasks):
            task.cancel()
        for future in tuple(self.pending):
            future.cancel()
        if self.worker:
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)
        await asyncio.gather(*tuple(self.primary_tasks), return_exceptions=True)
        while not self.queue.empty():
            pending = self.queue.get_nowait()
            pending.future.cancel()
            self.queue.task_done()

    def unavailable(self) -> Measurement:
        return Measurement(
            outcome="unavailable",
            usage=BudgetState(policy=self.candidate_budget).summary(),
            elapsed_seconds=0,
        )

    async def _measure(
        self, snapshot: ShadowSnapshot, workflow: Workflow, budget: BudgetPolicy, seconds: float
    ) -> Measurement:
        ledger = MemoryBudget(BudgetState(policy=budget))
        effects = SimulatedTicket()
        started = asyncio.get_running_loop().time()
        fixture = snapshot.fixture.model_copy(
            update={
                "principal": snapshot.fixture.principal.model_copy(
                    update={"scopes": snapshot.fixture.principal.scopes & READ_SCOPES}
                )
            }
        )
        try:
            async with asyncio.timeout(seconds):
                with (
                    budget_scope(ledger),
                    memory_source_scope(SnapshotReader(snapshot), lambda: fixture.principal),
                    ticket_scope(effects),
                ):
                    observed = await execute_fixture(fixture, workflow, tool_delay=0.02)
            quality = check_observation(fixture, observed)
            if observed.result:
                ContentPolicy(fixture.protected_values).check(observed.result.model_dump_json())
            return Measurement(
                outcome="succeeded" if observed.result else "rejected",
                result=observed.result,
                quality=quality,
                usage=ledger.state.summary(),
                elapsed_seconds=asyncio.get_running_loop().time() - started,
                simulated_effects=effects.calls,
            )
        except TimeoutError:
            outcome: Literal["timeout", "rejected"] = "timeout"
        except Exception:
            outcome = "rejected"
        return Measurement(
            outcome=outcome,
            usage=ledger.state.summary(),
            elapsed_seconds=asyncio.get_running_loop().time() - started,
        )

    async def _worker(self) -> None:
        while True:
            work = await self.queue.get()
            active: asyncio.Task[Measurement] | None = None
            try:
                remaining = work.expires - asyncio.get_running_loop().time()
                if work.future.done():
                    continue
                if remaining <= 0:
                    work.future.set_result(self.unavailable())
                    continue
                active = asyncio.create_task(
                    self._measure(
                        work.snapshot, self.candidate.workflow, self.candidate_budget, remaining
                    ),
                    context=Context(),
                )

                callback_task: asyncio.Task[Measurement] = active

                def cancelled(
                    future: asyncio.Future[Measurement],
                    task: asyncio.Task[Measurement] = callback_task,
                ) -> None:
                    if future.cancelled() and not task.cancelling():
                        task.cancel()

                work.future.add_done_callback(cancelled)
                try:
                    measured = await active
                    if not work.future.done():
                        queued = max(0, self.timeout - remaining)
                        measured = measured.model_copy(
                            update={
                                "queue_wait_seconds": queued,
                                "elapsed_seconds": measured.elapsed_seconds + queued,
                            }
                        )
                        work.future.set_result(measured)
                except asyncio.CancelledError:
                    if not work.future.done():
                        work.future.cancel()
                    worker = asyncio.current_task()
                    if worker is not None and worker.cancelling():
                        raise
            finally:
                if active and not active.done():
                    active.cancel()
                    await asyncio.gather(active, return_exceptions=True)
                self.queue.task_done()

    async def compare(self, snapshot: ShadowSnapshot) -> Pair:
        if self.closed or self.worker is None or self.worker.done():
            raise ValueError("SHADOW_UNAVAILABLE")
        if len(self.primary_tasks) + len(self.pending) >= self.queue.maxsize + 2:
            raise ValueError("SHADOW_BUSY")
        # Revalidate serialized copies so model_copy(update=...) cannot bypass admission policy.
        snapshot = ShadowSnapshot.model_validate_json(snapshot.model_dump_json())
        primary_task = asyncio.create_task(
            self._measure(snapshot, self.primary.workflow, self.budget, self.timeout),
            context=Context(),
        )
        self.primary_tasks.add(primary_task)
        try:
            primary = await primary_task
        finally:
            self.primary_tasks.discard(primary_task)
        if self.closed:
            raise ValueError("SHADOW_UNAVAILABLE")
        future: asyncio.Future[Measurement] = asyncio.get_running_loop().create_future()
        try:
            self.queue.put_nowait(
                Work(snapshot, asyncio.get_running_loop().time() + self.timeout, future)
            )
        except asyncio.QueueFull:
            return Pair(
                case=snapshot.fixture.name,
                snapshot_sha256=snapshot.digest(),
                primary=primary,
                candidate=self.unavailable(),
            )
        try:
            self.pending.add(future)
            candidate = await future
            return Pair(
                case=snapshot.fixture.name,
                snapshot_sha256=snapshot.digest(),
                primary=primary,
                candidate=candidate,
            )
        finally:
            self.pending.discard(future)
            if not future.done():
                future.cancel()


class ReleaseSelection(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    active: WorkflowPin
    rollback: WorkflowPin | None = None


def promote(selection: ReleaseSelection, report: dict[str, object]) -> ReleaseSelection:
    primary = WorkflowPin.model_validate(report["primary_pin"])
    candidate = WorkflowPin.model_validate(report["candidate_pin"])
    gates = report.get("gates")
    if (
        report.get("promotion_eligible") is not True
        or not isinstance(gates, dict)
        or set(gates) != GATE_NAMES
        or not all(value is True for value in gates.values())
        or selection.active != primary
        or primary != pin(primary.workflow)
        or candidate != pin(candidate.workflow)
        or report.get("policy") != "offline-shadow-v1"
        or report.get("fixture_sha256") != load_fixtures()[1]
    ):
        raise ValueError("PROMOTION_REJECTED")
    return ReleaseSelection(active=candidate, rollback=primary)


def rollback(selection: ReleaseSelection) -> ReleaseSelection:
    if selection.rollback is None or selection.rollback != pin(selection.rollback.workflow):
        raise ValueError("ROLLBACK_PIN_MISMATCH")
    return ReleaseSelection(active=selection.rollback, rollback=selection.active)


async def report(
    primary: Workflow = "baseline", candidate: Workflow = "coordinated"
) -> dict[str, object]:
    fixtures, digest = await asyncio.to_thread(load_fixtures)
    primary_pin, candidate_pin = (
        await asyncio.to_thread(pin, primary),
        await asyncio.to_thread(pin, candidate),
    )
    evaluations = {
        workflow: await evaluate(workflow) for workflow in dict.fromkeys((primary, candidate))
    }
    pairs: list[Pair] = []
    excluded: list[str] = []
    async with ShadowReplay(primary_pin, candidate_pin) as replay:
        for fixture in fixtures.cases:
            try:
                snapshot = ShadowSnapshot(fixture=fixture)
            except (PolicyDenied, ValidationError):
                excluded.append(fixture.name)
                continue
            pairs.append(await replay.compare(snapshot))
    gates = {
        "full_evaluation_passed": all(e["passed"] is True for e in evaluations.values()),
        "all_paired_quality_passed": bool(pairs)
        and all(
            all(p.primary.quality.values())
            and all(p.candidate.quality.values())
            and bool(p.candidate.quality)
            for p in pairs
        ),
        "outcome_and_evidence_parity": all(
            p.primary.outcome == p.candidate.outcome and p.primary.result == p.candidate.result
            for p in pairs
        ),
        "candidate_estimated_cost_at_most_105_percent": all(
            p.candidate.usage.estimated_cost_usd <= p.primary.usage.estimated_cost_usd * 105 / 100
            for p in pairs
        ),
        "candidate_latency_at_most_2x_plus_50ms": all(
            p.candidate.elapsed_seconds <= p.primary.elapsed_seconds * 2 + 0.05 for p in pairs
        ),
    }
    return {
        "policy": "offline-shadow-v1",
        "primary_pin": primary_pin.model_dump(),
        "candidate_pin": candidate_pin.model_dump(),
        "fixture_sha256": digest,
        "model": "fake-v1",
        "effects": "ticket proposals simulated; no DB or external effect adapters supplied",
        "initial_state": "same immutable authorized input, read fixtures and private memory "
        "snapshot; fresh checkpoints and independent budget ledgers",
        "candidate_queue_capacity": 2,
        "candidate_workers": 1,
        "candidate_deadline_seconds": 5,
        "primary_results_only": True,
        "gates": gates,
        "promotion_eligible": all(gates.values()),
        "full_evaluations": evaluations,
        "pairs": [{**p.model_dump(mode="json"), "differences": p.differences()} for p in pairs],
        "excluded_unsanitized_cases": excluded,
        "host": {"platform": platform.platform(), "python": platform.python_version()},
        "limitations": "Offline fake-model replay with synthetic $1/$2 input/output per-million "
        "pricing. Timings are illustrative, not live-model quality or production capacity. "
        "Promotion requires an explicit operator command and restart; "
        "no traffic is switched automatically.",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--primary", choices=("baseline", "pydanticai", "coordinated"), default="baseline"
    )
    parser.add_argument(
        "--candidate", choices=("baseline", "pydanticai", "coordinated"), default="coordinated"
    )
    parser.add_argument("--selection", type=Path)
    parser.add_argument(
        "--promote", type=Path, help="Explicitly promote from this local reviewed report"
    )
    parser.add_argument("--rollback", action="store_true")
    parser.add_argument("--check-selection", action="store_true")
    args = parser.parse_args()
    if args.promote or args.rollback or args.check_selection:
        if args.selection is None:
            parser.error("--selection is required")
        selection = ReleaseSelection.model_validate_json(args.selection.read_text("utf-8"))
        if args.check_selection:
            if selection.active != pin(selection.active.workflow):
                raise ValueError("RELEASE_PIN_MISMATCH")
            print(selection.active.workflow)
            return 0
        selection = (
            promote(selection, json.loads(args.promote.read_text("utf-8")))
            if args.promote
            else rollback(selection)
        )
        args.selection.write_text(selection.model_dump_json(indent=2) + "\n", encoding="utf-8")
        return 0
    if args.output is None:
        parser.error("--output is required")
    result = asyncio.run(report(args.primary, args.candidate))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if args.selection:
        if args.selection.exists():
            parser.error("Selection already exists; use explicit promotion or rollback")
        args.selection.write_text(
            ReleaseSelection(
                active=WorkflowPin.model_validate(result["primary_pin"])
            ).model_dump_json(indent=2)
            + "\n",
            encoding="utf-8",
        )
    return 0 if result["promotion_eligible"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
