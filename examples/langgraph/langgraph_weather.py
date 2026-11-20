"""A LangGraph ReAct agent (`create_react_agent`) with `langchain-openai` and one tool.

`ChatOpenAI` talks to RunWeave's LLM proxy (`ctx.proxy`). In mock mode the proxy answers
from fixtures.json, so the graph runs offline with no API key.
"""

from langchain_core.messages import AIMessage
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from langgraph.prebuilt import create_react_agent
from pydantic import BaseModel, Field, SecretStr

from runweave.sdk import AgentUnavailable, RunContext, agent

FORECASTS = {"paris": "sunny, 21 °C", "london": "light rain, 14 °C", "tokyo": "cloudy, 18 °C"}
# Each ReAct turn is a model node and a tool node, so 8 graph steps allow 4 model calls.
RECURSION_LIMIT = 8


class Question(BaseModel):
    question: str = Field(min_length=1, max_length=2000)


class Answer(BaseModel):
    answer: str


@agent(
    name="langgraph-weather",
    version="0.1.0",
    input=Question,
    output=Answer,
    description="LangGraph create_react_agent with langchain-openai, through the proxy.",
    max_steps=10,
)
async def run(input: Question, ctx: RunContext) -> Answer:
    if ctx.proxy is None:
        raise AgentUnavailable()

    @tool
    async def get_weather(city: str) -> str:
        """Current weather for a city."""
        async with ctx.step("get_weather", detail=city[:100]):
            return f"{city}: {FORECASTS.get(city.casefold(), 'unknown')}"

    model = ChatOpenAI(
        model="gpt-4.1-mini",
        api_key=SecretStr(ctx.proxy.token),
        base_url=ctx.proxy.openai_base_url,
        max_retries=2,
    )
    graph = create_react_agent(
        model, [get_weather], prompt="Answer weather questions with the get_weather tool."
    )
    state = await graph.ainvoke(
        {"messages": [("user", input.question)]}, {"recursion_limit": RECURSION_LIMIT}
    )
    final = state["messages"][-1]
    if not isinstance(final, AIMessage):
        raise AgentUnavailable()
    return Answer(answer=final.text)
