"""Contract fixture agent (Python). Behavior is specified in tests/contract/README.md.

Run: python tests/contract/python_agent.py  ->  prints "RUNWEAVE_AGENT_READY <url>".
"""

import asyncio
import json
from typing import Any

from pydantic import BaseModel, Field
from starlette.types import ASGIApp, Receive, Scope, Send

from runweave.sdk import EffectResponseLost, RunContext, agent
from runweave.sdk_http import create_agent_app, serve_agents


class EchoInput(BaseModel):
    text: str = Field(min_length=1, max_length=100)
    delay_ms: int = Field(default=0, ge=0, le=60000)


class EchoOutput(BaseModel):
    reply: str
    attempt: int


events: dict[str, Any] = {"cancelled": [], "requests": {}, "approvals": [], "tickets": {}}


@agent(name="contract-echo", version="1.0.0", input=EchoInput, output=EchoOutput, max_steps=5)
async def echo(input: EchoInput, ctx: RunContext) -> EchoOutput:
    async with ctx.step("Echo", detail=f"{len(input.text)} characters"):
        try:
            await asyncio.sleep(input.delay_ms / 1000)
        except asyncio.CancelledError:
            events["cancelled"].append(input.text)
            raise
    return EchoOutput(reply=input.text, attempt=ctx.attempt)


@agent(name="contract-crash", version="1.0.0", input=EchoInput, output=EchoOutput)
async def crash(input: EchoInput, ctx: RunContext) -> EchoOutput:
    raise RuntimeError("fixture crash with /internal/path")


class ApproveInput(BaseModel):
    order: str = Field(min_length=1, max_length=40)
    expires_in: float = Field(default=30, gt=0, le=3600)


class ApproveOutput(BaseModel):
    approved: bool
    status: str
    decided_by: str | None
    attempt: int
    remaining_ms: float


@agent(name="contract-approver", version="1.0.0", input=ApproveInput, output=ApproveOutput)
async def approver(input: ApproveInput, ctx: RunContext) -> ApproveOutput:
    events["approvals"].append({"order": input.order, "attempt": ctx.attempt})
    decision = await ctx.request_approval(
        "issue_refund", {"order": input.order}, expires_in=input.expires_in
    )
    return ApproveOutput(
        approved=decision.approved,
        status=decision.status,
        decided_by=decision.decided_by,
        attempt=ctx.attempt,
        remaining_ms=ctx.remaining_seconds() * 1000,
    )


class TicketInput(BaseModel):
    order: str = Field(min_length=1, max_length=40)
    use_key: bool = True


class TicketOutput(BaseModel):
    ticket: str
    tries: int


async def _create_ticket(order: str, key: str | None) -> dict[str, str]:
    """A downstream that honours idempotency keys."""
    tickets = events["tickets"].setdefault(order, [])
    for ticket in tickets:
        if key is not None and ticket["key"] == key:
            return {"id": ticket["id"]}
    ticket = {"id": f"{order}-{len(tickets) + 1}", "key": key}
    tickets.append(ticket)
    return {"id": ticket["id"]}


@agent(name="contract-ticketing", version="1.0.0", input=TicketInput, output=TicketOutput)
async def ticketing(input: TicketInput, ctx: RunContext) -> TicketOutput:
    key = f"order-{input.order}" if input.use_key else None
    for tries in range(1, 6):
        try:
            ticket = await ctx.effect(
                "create_ticket",
                lambda: _create_ticket(input.order, key),
                key=key,
                resource=lambda t: t["id"],
            )
            return TicketOutput(ticket=ticket["id"], tries=tries)
        except EffectResponseLost:
            continue
    raise RuntimeError("no ticket after 5 tries")


def _misbehaving(name: str) -> Any:
    @agent(name=name, version="1.0.0", input=EchoInput, output=EchoOutput)
    async def unreachable(input: EchoInput, ctx: RunContext) -> EchoOutput:
        raise AssertionError("the fixture answers this agent's runs itself")

    return unreachable


RAW = {
    "contract-malformed": (200, {"Content-Type": "application/json"}, b'{"output": '),
    "contract-oversized": (
        200,
        {"Content-Type": "application/json"},
        json.dumps({"output": {"reply": "x" * 2_000_000, "attempt": 1}}).encode(),
    ),
    "contract-redirect": (307, {"Location": "http://169.254.169.254/latest/meta-data"}, b""),
    "contract-unavailable": (503, {"Retry-After": "0"}, b""),
    "contract-server-error": (500, {"Content-Type": "text/plain"}, b"Traceback /secret/path"),
}


class Fixture:
    """Answers misbehaving agents' runs directly and exposes what the fixture observed."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "") if scope["type"] == "http" else ""
        if path == "/fixture/events":
            await _respond(
                send, 200, {"Content-Type": "application/json"}, json.dumps(events).encode()
            )
            return
        parts = path.strip("/").split("/")
        if len(parts) == 5 and parts[:3] == ["runweave", "v1", "agents"] and parts[4] == "runs":
            name = parts[3]
            events["requests"][name] = events["requests"].get(name, 0) + 1
            if name in RAW:
                await _respond(send, *RAW[name])
                return
        await self.app(scope, receive, send)


async def _respond(send: Send, status: int, headers: dict[str, str], body: bytes) -> None:
    raw = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    raw.append((b"content-length", str(len(body)).encode()))
    await send({"type": "http.response.start", "status": status, "headers": raw})
    await send({"type": "http.response.body", "body": body})


app = Fixture(
    create_agent_app(echo, crash, approver, ticketing, *(_misbehaving(name) for name in RAW))
)

if __name__ == "__main__":
    asyncio.run(serve_agents(app, port=0))
