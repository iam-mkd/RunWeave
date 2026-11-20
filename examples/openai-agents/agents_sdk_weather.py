"""An OpenAI Agents SDK agent with one `function_tool`, on the Responses API.

The SDK's model talks to RunWeave's LLM proxy (`ctx.proxy`) and tracing is off, so nothing
is sent to OpenAI. In mock mode the proxy answers from fixtures.json: offline, no API key.
"""

from agents import Agent, OpenAIResponsesModel, RunConfig, Runner, function_tool
from openai import AsyncOpenAI
from pydantic import BaseModel, Field

from runweave.sdk import AgentUnavailable, RunContext, agent

FORECASTS = {"paris": "sunny, 21 °C", "london": "light rain, 14 °C", "tokyo": "cloudy, 18 °C"}
MAX_TURNS = 4


class Question(BaseModel):
    question: str = Field(min_length=1, max_length=2000)


class Answer(BaseModel):
    answer: str


@agent(
    name="openai-agents-weather",
    version="0.1.0",
    input=Question,
    output=Answer,
    description="OpenAI Agents SDK with a function tool, on the proxy's Responses API.",
    max_steps=10,
)
async def run(input: Question, ctx: RunContext) -> Answer:
    if ctx.proxy is None:
        raise AgentUnavailable()

    @function_tool
    async def get_weather(city: str) -> str:
        """Current weather for a city.

        Args:
            city: The city to look up.
        """
        async with ctx.step("get_weather", detail=city[:100]):
            return f"{city}: {FORECASTS.get(city.casefold(), 'unknown')}"

    async with AsyncOpenAI(api_key=ctx.proxy.token, base_url=ctx.proxy.openai_base_url) as client:
        weather_agent = Agent(
            name="Weather",
            instructions="Answer weather questions with the get_weather tool.",
            model=OpenAIResponsesModel(model="gpt-4.1-mini", openai_client=client),
            tools=[get_weather],
        )
        result = await Runner.run(
            weather_agent,
            input.question,
            max_turns=MAX_TURNS,
            run_config=RunConfig(tracing_disabled=True),
        )
    return Answer(answer=str(result.final_output))
