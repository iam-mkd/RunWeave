"""A small, deterministic agent showing the RunWeave Python SDK.

Run it with:  runweave serve --agent sample_agent:text_report
(`refund_request` asks for approval and needs RUNWEAVE_DATABASE_URL.)
"""

import asyncio
import re

from pydantic import BaseModel, Field

from runweave.sdk import RunContext, agent


class TextInput(BaseModel):
    text: str = Field(min_length=1, max_length=5000, description="Text to analyse.")
    keywords: list[str] = Field(
        default_factory=list, max_length=20, description="Words to count (case-insensitive)."
    )


class TextReport(BaseModel):
    words: int
    sentences: int
    keyword_hits: dict[str, int]
    summary: str


@agent(
    name="text-report",
    version="1.0.0",
    input=TextInput,
    output=TextReport,
    description="Counts words, sentences and keywords. Offline and deterministic.",
    max_steps=5,
)
async def text_report(input: TextInput, ctx: RunContext) -> TextReport:
    async with ctx.step("Tokenize", detail=f"{len(input.text)} characters"):
        await asyncio.sleep(0.05)  # Stands in for an I/O call such as a model or tool request.
        words = re.findall(r"[\w']+", input.text.lower())
        sentences = max(1, len(re.findall(r"[.!?]+", input.text)))
    async with ctx.step("Count keywords", detail=", ".join(input.keywords) or "none"):
        await asyncio.sleep(0.05)
        hits = {keyword: words.count(keyword.lower()) for keyword in input.keywords}
    ctx.log(f"Analysed {len(words)} words")
    return TextReport(
        words=len(words),
        sentences=sentences,
        keyword_hits=hits,
        summary=f"{len(words)} words in {sentences} sentence(s).",
    )


class RefundInput(BaseModel):
    order: str = Field(min_length=1, max_length=40, description="Order to refund.")
    amount: int = Field(ge=1, le=10000, description="Amount in whole currency units.")
    note: str = Field(default="", max_length=200, description="Shown to the reviewer.")


class RefundOutcome(BaseModel):
    approved: bool
    status: str
    decided_by: str | None


@agent(
    name="refund-request",
    version="1.0.0",
    input=RefundInput,
    output=RefundOutcome,
    description="Asks a person to approve a refund before issuing it (needs durable mode).",
    max_steps=3,
)
async def refund_request(input: RefundInput, ctx: RunContext) -> RefundOutcome:
    decision = await ctx.request_approval(
        "issue_refund",
        {"order": input.order, "amount": input.amount, "note": input.note},
        expires_in=3600,
    )
    if decision.approved:
        async with ctx.step("Issue refund", detail=f"{input.order}: {input.amount}"):
            # A real agent would call its payments API here with an idempotency key, for
            # example str(ctx.run_id), so a re-run after a restart cannot refund twice.
            await asyncio.sleep(0.01)
    return RefundOutcome(
        approved=decision.approved, status=decision.status, decided_by=decision.decided_by
    )
