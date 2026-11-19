"""`ctx.request_approval` without PostgreSQL, and the deadline pause it relies on."""

import asyncio

import httpx
import pytest
from pydantic import BaseModel

from runweave.action_approvals import ApprovalUnavailable, argument_digest
from runweave.agents.catalog import PythonAgent
from runweave.app import create_app
from runweave.core.settings import Settings
from runweave.deadline import RUN_DEADLINE, deadline_scope, paused_deadline, remaining_budget
from runweave.sdk import RunContext, agent


class Ask(BaseModel):
    order: str


class Answer(BaseModel):
    approved: bool


@agent(name="asks", version="1", input=Ask, output=Answer)
async def asks(input: Ask, ctx: RunContext) -> Answer:
    decision = await ctx.request_approval("ship", {"order": input.order})
    return Answer(approved=decision.approved)


@pytest.mark.asyncio
async def test_memory_mode_rejects_approvals_with_a_clear_error() -> None:
    with pytest.raises(ApprovalUnavailable, match="RUNWEAVE_DATABASE_URL"):
        await RunContext.detached(agent="asks").request_approval("ship", {"order": "x"})
    settings = Settings(_env_file=None, model_provider="fake", key_store="none")  # type: ignore[call-arg]
    app = create_app(
        settings,
        agents=[PythonAgent.from_function(asks, target="tests.test_action_approvals:asks")],
    )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://rw") as client,
    ):
        url = (
            await client.post("/v1/runs", json={"agent": "asks", "input": {"order": "x"}})
        ).json()["status_url"]
        async with asyncio.timeout(10):
            while True:
                run = (await client.get(url)).json()
                if run["status"] in {"FAILED", "SUCCEEDED"}:
                    break
                await asyncio.sleep(0.02)
        listing = await client.get("/v1/approvals")
    assert run["status"] == "FAILED" and run["error"]["code"] == "DURABLE_APPROVAL_REQUIRED"
    assert listing.status_code == 200 and listing.json() == {"approvals": [], "durable": False}


def test_action_names_and_arguments_are_validated() -> None:
    assert argument_digest({"b": 1, "a": [1, 2]}) == argument_digest({"a": [1, 2], "b": 1})
    assert len(argument_digest({})) == 64
    with pytest.raises(ValueError):
        argument_digest({"too": "x" * 70000})
    with pytest.raises(ValueError):
        argument_digest({"not": object()})


@pytest.mark.asyncio
async def test_a_paused_deadline_does_not_fire_and_keeps_the_remaining_time() -> None:
    loop = asyncio.get_running_loop()
    async with deadline_scope(loop.time() + 0.3, RUN_DEADLINE):
        before = remaining_budget()
        async with paused_deadline(loop.time() + 5):
            await asyncio.sleep(0.5)  # longer than the run deadline
        after = remaining_budget()
        assert before - 0.05 <= after <= before
    with pytest.raises(TimeoutError):
        async with deadline_scope(loop.time() + 0.2, RUN_DEADLINE):
            async with paused_deadline(loop.time() + 0.05):
                await asyncio.sleep(0.01)
            await asyncio.sleep(1)  # the remaining time still runs out afterwards


@pytest.mark.asyncio
async def test_paused_deadline_notifies_listeners_of_the_shift() -> None:
    loop = asyncio.get_running_loop()
    shifts: list[float] = []
    async with deadline_scope(loop.time() + 1, RUN_DEADLINE):
        from runweave.deadline import current_deadline

        budget = current_deadline()
        assert budget is not None
        budget.listeners.append(shifts.append)
        async with paused_deadline(loop.time() + 1):
            await asyncio.sleep(0.2)
    assert len(shifts) == 1 and 0.15 <= shifts[0] <= 0.5
