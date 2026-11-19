"""A small agent to start from. Replace the body of `run` with your own logic."""

from pydantic import BaseModel, Field

from runweave.sdk import RunContext, agent


class WordCountInput(BaseModel):
    text: str = Field(min_length=1, max_length=5000, description="Text to analyse")


class WordCount(BaseModel):
    words: int
    longest: str


@agent(
    name="word-count",
    version="0.1.0",
    input=WordCountInput,
    output=WordCount,
    description="Counts words and finds the longest one. Offline and deterministic.",
    max_steps=5,
)
async def run(input: WordCountInput, ctx: RunContext) -> WordCount:
    async with ctx.step("Split words"):
        words = input.text.split()
    async with ctx.step("Find the longest word"):
        longest = max(words, key=len) if words else ""
    # To call a model, point your SDK at ctx.proxy (see docs/llm-proxy.md).
    return WordCount(words=len(words), longest=longest)
