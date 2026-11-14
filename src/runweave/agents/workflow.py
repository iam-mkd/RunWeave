from pydantic_ai import (  # type: ignore[import-not-found,unused-ignore]
    Agent,
    DeferredToolRequests,
    ModelMessage,
    ModelResponse,
    ToolCallPart,
    ToolOutput,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel  # type: ignore[import-not-found,unused-ignore]
from pydantic_ai.tools import ToolDefinition  # type: ignore[import-not-found,unused-ignore]
from pydantic_ai.toolsets import ExternalToolset  # type: ignore[import-not-found,unused-ignore]
from pydantic_ai.usage import RequestUsage, UsageLimits  # type: ignore[import-not-found,unused-ignore]
from pydantic_graph.graph_builder import Graph, GraphBuilder  # type: ignore[import-not-found,unused-ignore]
from pydantic_graph.step import StepContext  # type: ignore[import-not-found,unused-ignore]

from runweave.agents.contracts import (
    AgentModel,
    IncidentRequest,
    IncidentResult,
    ModelDecision,
    ToolCall,
    ToolExchange,
)
from runweave.agents.groq import FINISH
from runweave.agents.triage import TriageState, TriageSteps, validate_triage_result
from runweave.tools.contracts import HealthQuery
from runweave.tools.registry import HEALTH, RUNBOOK


class PydanticDecisionModel:
    """Use PydanticAI for tool proposals and output validation, with RunWeave transport."""

    def __init__(self, provider: AgentModel) -> None:
        self._provider = provider
        self._agent: Agent[None, IncidentResult | DeferredToolRequests] = Agent(
            output_type=[
                ToolOutput(IncidentResult, name=FINISH),
                DeferredToolRequests,
            ],
            toolsets=[
                ExternalToolset(
                    [
                        ToolDefinition(
                            name=name, parameters_json_schema=HealthQuery.model_json_schema()
                        )
                        for name in (HEALTH, RUNBOOK)
                    ]
                )
            ],
            retries=0,
        )

    async def decide(
        self,
        incident: IncidentRequest,
        history: tuple[ToolExchange, ...],
        tools: tuple[dict[str, object], ...],
        completion_budget: int,
    ) -> ModelDecision:
        used = 0

        async def request(_messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
            nonlocal used
            if used:
                raise ValueError("Expected one provider request per triage step")
            decision = await self._provider.decide(incident, history, tools, completion_budget)
            if not 0 < decision.completion_tokens <= completion_budget:
                raise ValueError("Invalid completion token usage")
            used = decision.completion_tokens
            action = decision.action
            part = (
                ToolCallPart(FINISH, action.model_dump_json())
                if isinstance(action, IncidentResult)
                else ToolCallPart(action.name, action.arguments, tool_call_id=action.id)
            )
            return ModelResponse(parts=[part], usage=RequestUsage(output_tokens=used))

        result = await self._agent.run(
            incident.model_dump_json(),
            model=FunctionModel(request),
            usage_limits=UsageLimits(request_limit=1),
        )
        output = result.output
        if isinstance(output, IncidentResult):
            return ModelDecision(output, used)
        if output.approvals or len(output.calls) != 1:
            raise ValueError("Expected one deferred triage tool call")
        call = output.calls[0]
        return ModelDecision(
            ToolCall(id=call.tool_call_id, name=call.tool_name, arguments=call.args_as_json_str()),
            used,
        )


class PydanticWorkflow:
    def __init__(self, steps: TriageSteps, max_steps: int, completion_budget: int) -> None:
        self._steps = steps
        self._max_steps = max_steps
        self._completion_budget = completion_budget
        self._graph = self._build_graph()

    @staticmethod
    def _build_graph() -> Graph[TriageState, TriageSteps, None, IncidentResult]:  # type: ignore[type-arg,unused-ignore]
        builder = GraphBuilder(
            state_type=TriageState,
            deps_type=TriageSteps,
            output_type=IncidentResult,
            auto_instrument=False,
        )

        @builder.step  # type: ignore[untyped-decorator,unused-ignore]
        async def decide(
            ctx: StepContext[TriageState, TriageSteps, None],  # type: ignore[type-arg,unused-ignore]
        ) -> ToolCall | IncidentResult:
            steps: TriageSteps = ctx.deps
            return await steps.decide(ctx.state)

        @builder.step  # type: ignore[untyped-decorator,unused-ignore]
        async def collect(
            ctx: StepContext[TriageState, TriageSteps, ToolCall],  # type: ignore[type-arg,unused-ignore]
        ) -> None:
            steps: TriageSteps = ctx.deps
            await steps.collect(ctx.state, ctx.inputs)

        @builder.step  # type: ignore[untyped-decorator,unused-ignore]
        async def finish(
            ctx: StepContext[TriageState, TriageSteps, IncidentResult],  # type: ignore[type-arg,unused-ignore]
        ) -> IncidentResult:
            return validate_triage_result(ctx.state, ctx.inputs)

        builder.add(
            builder.edge_from(builder.start_node).to(decide),
            builder.edge_from(decide).to(
                builder.decision()
                .branch(builder.match(ToolCall).to(collect))
                .branch(builder.match(IncidentResult).to(finish))
            ),
            builder.edge_from(collect).to(decide),
            builder.edge_from(finish).to(builder.end_node),
        )
        return builder.build()

    async def run(self, incident: IncidentRequest) -> IncidentResult:
        state = TriageState(incident, self._max_steps, self._completion_budget)
        try:
            result: IncidentResult = await self._graph.run(state=state, deps=self._steps)
            return result
        except ExceptionGroup as group:
            # The triage graph is serial, so preserve the underlying step error for the scheduler.
            error: Exception = group
            while isinstance(error, ExceptionGroup) and len(error.exceptions) == 1:
                error = error.exceptions[0]
            if error is group:
                raise
            raise error from None
