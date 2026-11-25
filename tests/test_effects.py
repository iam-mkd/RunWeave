"""Effects: a side-effecting call routed through RunWeave, and a lost response after the write.

The failure per-call fault injection cannot see: the downstream processed the write, but
its response never arrived, so the agent retries and may write twice.
"""

import asyncio
import hashlib
import json
import os
import subprocess
import sys
from functools import partial
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest
from pydantic import BaseModel

from runweave.agent_effects import (
    EffectFaults,
    call_counts,
    duplicate_writes,
    effect_faults_scope,
)
from runweave.agents.catalog import PythonAgent
from runweave.app import create_app
from runweave.core.settings import Settings
from runweave.proxy.faults import BUILT_IN_PROFILES, FaultPlan, FaultProfile
from runweave.proxy.mock import ModelRequest
from runweave.sdk import EffectResponseLost, RunContext, agent
from runweave.steps import StepEvent, step_scope


class Timeline:
    """A StepLog that keeps every finished event, like the in-memory registry."""

    def __init__(self) -> None:
        self.events: list[StepEvent] = []

    async def begin(self, title: str, detail: str | None) -> StepEvent | None:
        from datetime import UTC, datetime

        return StepEvent(
            index=len(self.events), title=title, detail=detail, status="running",
            started_at=datetime.now(UTC),
        )  # fmt: skip

    async def end(self, event: StepEvent) -> None:
        self.events.append(event)

    def dumped(self) -> list[dict[str, Any]]:
        return [event.model_dump(mode="json") for event in self.events]


class TicketService:
    """A downstream that honours idempotency keys, as a well-behaved API does."""

    def __init__(self) -> None:
        self.tickets: dict[str, dict[str, str]] = {}
        self.by_key: dict[str, str] = {}

    async def create(self, title: str, key: str | None = None) -> dict[str, str]:
        if key is not None and key in self.by_key:
            return self.tickets[self.by_key[key]]
        ticket = {"id": f"T-{len(self.tickets) + 1}", "title": title}
        self.tickets[ticket["id"]] = ticket
        if key is not None:
            self.by_key[key] = ticket["id"]
        return ticket


def context() -> RunContext:
    return RunContext(run_id=uuid4(), agent="test")


class LoseFirst:
    """Loses the response of the attempt's first effect call, and keeps the rest."""

    def lost(self, position: int, name: str) -> bool:
        return position == 0


def always_lose_first() -> LoseFirst:
    return LoseFirst()


async def retrying_agent(ctx: RunContext, service: TicketService, *, key: str | None) -> Any:
    for _ in range(3):
        try:
            return await ctx.effect(
                "create_ticket",
                lambda: service.create("Printer on fire", key),
                key=key,
                resource=lambda ticket: ticket["id"],
            )
        except EffectResponseLost:
            continue
    raise AssertionError("never succeeded")


# The fault and its draw


def model_request() -> ModelRequest:
    return ModelRequest(
        api="chat", model="m", last_role="user", last_user_text="hi", tool_names=("t",),
        stream=True, include_usage=False, max_tokens=None, payload_bytes=100,
    )  # fmt: skip


def test_lost_writes_is_a_built_in_profile_and_chaos_is_unchanged() -> None:
    lost = BUILT_IN_PROFILES["lost-writes"]
    assert [fault.kind for fault in lost.faults] == ["response_lost"]
    assert "response_lost" not in {fault.kind for fault in BUILT_IN_PROFILES["chaos"].faults}


def test_response_lost_never_applies_to_model_calls() -> None:
    plan = FaultPlan(BUILT_IN_PROFILES["lost-writes"], seed=3)
    assert all(plan.choose(i, model_request()) is None for i in range(500))
    assert FaultPlan(BUILT_IN_PROFILES["chaos"], seed=3).effect_faults() is None
    assert FaultPlan(None, seed=3).effect_faults() is None


def test_the_effect_draw_is_seeded_and_documented() -> None:
    faults = FaultPlan(BUILT_IN_PROFILES["lost-writes"], seed=11).effect_faults()
    assert faults is not None
    expected = [
        n
        for n in range(64)
        if int.from_bytes(
            hashlib.sha256(f"runweave-effect-faults:11:{n}".encode()).digest()[:8], "big"
        )
        / 2**64
        < 0.5
    ]
    assert [n for n in range(64) if faults.lost(n, "create_ticket")] == expected
    assert 20 < len(expected) < 44


def test_an_effects_filter_limits_which_effects_lose_responses() -> None:
    profile = FaultProfile.model_validate(
        {"name": "t", "faults": [{"kind": "response_lost", "probability": 1,
                                  "effects": ["create_ticket"]}]}
    )  # fmt: skip
    faults = FaultPlan(profile, seed=0).effect_faults()
    assert faults is not None
    assert faults.lost(0, "create_ticket") and not faults.lost(0, "send_email")


def test_effect_faults_travel_as_json_for_http_agents() -> None:
    faults = FaultPlan(BUILT_IN_PROFILES["lost-writes"], seed=5).effect_faults()
    assert faults is not None
    again = EffectFaults.model_validate_json(faults.model_dump_json())
    assert [again.lost(n, "x") for n in range(32)] == [faults.lost(n, "x") for n in range(32)]
    assert json.loads(faults.model_dump_json())["seed"] == 5


# ctx.effect


async def test_outside_a_run_an_effect_just_runs() -> None:
    service = TicketService()
    ticket = await RunContext.detached(agent="t").effect(
        "create_ticket", lambda: service.create("x"), resource=lambda t: t["id"]
    )
    assert ticket["id"] == "T-1"


async def test_a_lost_response_comes_after_the_write_is_committed() -> None:
    service, timeline = TicketService(), Timeline()
    with step_scope(timeline), effect_faults_scope(always_lose_first()):
        with pytest.raises(EffectResponseLost) as lost:
            await context().effect("create_ticket", lambda: service.create("x"))
    assert isinstance(lost.value, TimeoutError)  # what an HTTP client's read timeout looks like
    assert list(service.tickets) == ["T-1"]  # the write happened
    [step] = timeline.dumped()
    assert step["kind"] == "effect" and step["effect"] == "create_ticket"
    assert step["status"] == "failed" and step["fault"] == "response_lost"
    assert step["resource"] and step["operation"]


async def test_a_retry_without_a_key_writes_twice_and_is_caught() -> None:
    service, timeline = TicketService(), Timeline()
    with step_scope(timeline), effect_faults_scope(always_lose_first()):
        ticket = await retrying_agent(context(), service, key=None)
    assert ticket["id"] == "T-2" and len(service.tickets) == 2
    assert duplicate_writes(timeline.dumped()) == ["create_ticket"]


async def test_a_retry_with_one_idempotency_key_writes_once() -> None:
    service, timeline = TicketService(), Timeline()
    with step_scope(timeline), effect_faults_scope(always_lose_first()):
        ticket = await retrying_agent(context(), service, key="order-17")
    assert ticket["id"] == "T-1" and len(service.tickets) == 1
    assert [step["status"] for step in timeline.dumped()] == ["failed", "succeeded"]
    assert duplicate_writes(timeline.dumped()) == []


async def test_distinct_keys_are_distinct_operations() -> None:
    service, timeline = TicketService(), Timeline()
    ctx = context()
    with step_scope(timeline):
        for key in ("a", "b"):
            await ctx.effect("create_ticket", partial(service.create, "x", key), key=key)
    assert len(service.tickets) == 2 and duplicate_writes(timeline.dumped()) == []


async def test_the_timeline_keeps_digests_never_values() -> None:
    service, timeline = TicketService(), Timeline()
    with step_scope(timeline):
        await context().effect(
            "create_ticket", lambda: service.create("secret title", "key-9"),
            key="key-9", resource=lambda t: t["id"],
        )  # fmt: skip
    text = json.dumps(timeline.dumped())
    assert "secret title" not in text and "key-9" not in text and "T-1" not in text


async def test_a_failing_call_records_no_write() -> None:
    timeline = Timeline()

    async def broken() -> None:
        raise ConnectionError("refused")

    with step_scope(timeline), pytest.raises(ConnectionError):
        await context().effect("create_ticket", broken)
    [step] = timeline.dumped()
    assert step["status"] == "failed" and step["resource"] is None and step["fault"] is None
    assert duplicate_writes(timeline.dumped()) == []


async def test_effect_names_are_validated() -> None:
    with pytest.raises(ValueError):
        await context().effect("Create Ticket!", TicketService().create)  # type: ignore[arg-type]


# In a real run: the active fault profile reaches ctx.effect


SERVICE = TicketService()


class Order(BaseModel):
    order: str


class Receipt(BaseModel):
    ticket: str


@agent(name="ticketing", version="1.0.0", input=Order, output=Receipt)
async def ticketing(input: Order, ctx: RunContext) -> Receipt:
    for _ in range(3):
        try:
            ticket = await ctx.effect(
                "create_ticket",
                lambda: SERVICE.create(input.order, f"order-{input.order}"),
                key=f"order-{input.order}",
                resource=lambda t: t["id"],
            )
            return Receipt(ticket=ticket["id"])
        except EffectResponseLost:
            continue
    raise RuntimeError("no ticket")


async def run_once(**settings: Any) -> dict[str, Any]:
    config = Settings(proxy_public_url="http://test", **settings)
    registered = [PythonAgent.from_function(ticketing, target="tests.test_effects:ticketing")]
    app = create_app(config, agents=registered)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        created = await client.post(
            "/v1/runs", json={"agent": "ticketing", "input": {"order": "A1"}}
        )
        assert created.status_code == 202, created.text
        for _ in range(200):
            run = (await client.get(f"/v1/runs/{created.json()['id']}")).json()
            if run["status"] not in {"QUEUED", "RUNNING"}:
                return dict(run)
            await asyncio.sleep(0.01)
    raise AssertionError("the run did not finish")


async def test_the_active_fault_profile_reaches_effects_in_a_run() -> None:
    lost = await run_once(proxy_fault_profile="lost-writes", proxy_fault_seed=0)
    effects = [step for step in lost["steps"] if step["kind"] == "effect"]
    first_lost = FaultPlan(BUILT_IN_PROFILES["lost-writes"], seed=0).effect_faults()
    assert first_lost is not None
    expected = [first_lost.lost(n, "create_ticket") for n in range(len(effects))]
    assert [step["fault"] == "response_lost" for step in effects] == expected
    assert lost["status"] == "SUCCEEDED" and duplicate_writes(lost["steps"]) == []

    calm = await run_once()
    assert [step["fault"] for step in calm["steps"] if step["kind"] == "effect"] == [None]


# Calls per tool: what max_tool_calls counts


def effect_step(name: str, operation: str, attempt: int = 1) -> dict[str, Any]:
    return {"title": name, "kind": "effect", "effect": name, "operation": operation,
            "status": "succeeded", "attempt": attempt}  # fmt: skip


def test_effects_count_operations_so_a_keyed_retry_is_one_call() -> None:
    retried = [effect_step("refund", "op-1"), effect_step("refund", "op-1")]
    assert call_counts(retried) == {"refund": 1}
    assert call_counts([*retried, effect_step("refund", "op-2")]) == {"refund": 2}


def test_plain_steps_count_each_call_even_failed_ones_and_across_attempts() -> None:
    steps = [
        {"title": "lookup", "kind": "step", "detail": None, "status": "failed", "attempt": 1},
        {"title": "lookup", "kind": "step", "detail": None, "status": "succeeded", "attempt": 2},
        {"title": "Tool call", "kind": "step", "detail": "search", "status": "succeeded"},
        {"title": "Model call", "kind": "model_call", "detail": "chat", "status": "succeeded"},
    ]
    counts = call_counts(steps)
    assert counts["lookup"] == 2 and counts["search"] == 1
    assert "Model call" not in counts and "Tool call" not in counts


def test_an_effect_inside_a_step_of_the_same_name_is_not_counted_twice() -> None:
    wrapper = {"title": "refund", "kind": "step", "detail": None, "status": "succeeded"}
    assert call_counts([wrapper, effect_step("refund", "op-1")]) == {"refund": 1}


async def test_a_keyed_retry_after_a_lost_response_is_one_call() -> None:
    service, timeline = TicketService(), Timeline()
    with step_scope(timeline), effect_faults_scope(always_lose_first()):
        await retrying_agent(context(), service, key="order-17")
    assert len(timeline.events) == 2 and call_counts(timeline.dumped()) == {"create_ticket": 1}


# The worked example: examples/exactly-once


EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "exactly-once"


def _example(*args: str) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("RUNWEAVE_")}
    env["PYTHONPATH"] = str(EXAMPLE.parents[1] / "src")
    return subprocess.run(
        [sys.executable, "-m", "runweave.cli", *args], cwd=EXAMPLE, env=env,
        capture_output=True, text=True, timeout=180, check=False,
    )  # fmt: skip


def test_the_example_passes_with_a_key_and_catches_the_retry_without_one(tmp_path: Path) -> None:
    safe = _example("eval", "--junit", str(tmp_path / "safe.xml"))
    assert safe.returncode == 0, safe.stdout + safe.stderr
    naive = _example("eval", "--config", "naive.toml", "--junit", str(tmp_path / "naive.xml"))
    assert naive.returncode == 1, naive.stdout + naive.stderr
    assert naive.stdout.count("exactly_once: wrote twice after a retry: create_ticket") == 3
