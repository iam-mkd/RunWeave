"""A small, deterministic agent showing the RunWeave Python SDK.

Run it with:  runweave serve --agent sample_agent:text_report
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
