"""Two agents that create a support ticket, and a downstream that honours idempotency keys.

Under the `lost-writes` fault profile, RunWeave lets the write happen and then drops the
response, as a real network can. Both agents retry. Only one of them is safe to retry.
"""

from pydantic import BaseModel, Field

from runweave.sdk import EffectResponseLost, RunContext, agent


class TicketService:
    """An in-memory ticket API. Like most real APIs it deduplicates by idempotency key."""

    def __init__(self) -> None:
        self.tickets: dict[str, dict[str, str]] = {}
        self._by_key: dict[str, str] = {}

    async def create(self, title: str, idempotency_key: str | None = None) -> dict[str, str]:
        if idempotency_key is not None and idempotency_key in self._by_key:
            return self.tickets[self._by_key[idempotency_key]]
        ticket = {"id": f"T-{len(self.tickets) + 1}", "title": title}
        self.tickets[ticket["id"]] = ticket
        if idempotency_key is not None:
            self._by_key[idempotency_key] = ticket["id"]
        return ticket


service = TicketService()


class Problem(BaseModel):
    order: str = Field(min_length=1, max_length=40, pattern=r"^[A-Za-z0-9-]+$")
    problem: str = Field(min_length=1, max_length=200)


class Ticket(BaseModel):
    ticket: str
    tries: int


async def _with_retries(ctx: RunContext, input: Problem, key: str | None) -> Ticket:
    for tries in range(1, 4):
        try:
            ticket = await ctx.effect(
                "create_ticket",
                lambda: service.create(f"Order {input.order}: {input.problem}", key),
                key=key,
                resource=lambda created: created["id"],
            )
            return Ticket(ticket=ticket["id"], tries=tries)
        except EffectResponseLost:
            continue  # the request timed out; was the ticket created? We cannot tell.
    raise RuntimeError("Could not create the ticket")


@agent(
    name="naive-ticketing",
    version="1.0.0",
    input=Problem,
    output=Ticket,
    description="Retries a timed-out write without an idempotency key: may write twice.",
)
async def naive_agent(input: Problem, ctx: RunContext) -> Ticket:
    return await _with_retries(ctx, input, key=None)


@agent(
    name="safe-ticketing",
    version="1.0.0",
    input=Problem,
    output=Ticket,
    description="Retries with one idempotency key per order: writes exactly once.",
)
async def safe_agent(input: Problem, ctx: RunContext) -> Ticket:
    return await _with_retries(ctx, input, key=f"ticket-for-order-{input.order}")
