from collections.abc import Awaitable, Callable

from pydantic_ai import (  # type: ignore[import-not-found,unused-ignore]
    Agent,
    DeferredToolRequests,
    DeferredToolResults,
    ModelMessage,
    ModelResponse,
    TextPart,
    ToolCallPart,
)
from pydantic_ai.messages import ModelMessagesTypeAdapter  # type: ignore[import-not-found,unused-ignore]
from pydantic_ai.models.function import AgentInfo, FunctionModel  # type: ignore[import-not-found,unused-ignore]
from pydantic_ai.usage import UsageLimits  # type: ignore[import-not-found,unused-ignore]

from runweave.approvals import ApprovalError, TicketArguments


async def ticket_agent(
    arguments: TicketArguments,
    call_id: str,
    perform: Callable[[TicketArguments], Awaitable[str]],
    messages: str | None = None,
) -> tuple[str, str | None]:
    """One deterministic deferred tool, without an inference/provider request."""
    agent: Agent[None, str | DeferredToolRequests] = Agent(
        output_type=[str, DeferredToolRequests], retries=0
    )
    agent.instrument = False
    history = ModelMessagesTypeAdapter.validate_json(messages) if messages else None
    if history is not None:
        response = history[-1] if history else None
        part = response.parts[0] if isinstance(response, ModelResponse) and response.parts else None
        if (
            len(history) != 2
            or not isinstance(response, ModelResponse)
            or len(response.parts) != 1
            or not isinstance(part, ToolCallPart)
            or part.tool_name != "create_ticket"
            or part.tool_call_id != call_id
            or part.args_as_dict() != {"ticket": arguments.model_dump(mode="json")}
        ):
            raise ApprovalError("APPROVAL_BINDING_MISMATCH")

    @agent.tool_plain(requires_approval=True)  # type: ignore[untyped-decorator,unused-ignore]
    async def create_ticket(ticket: TicketArguments) -> str:
        if ticket != arguments:
            raise ApprovalError("APPROVAL_BINDING_MISMATCH")
        return await perform(ticket)

    async def request(_messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        if messages is None:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "create_ticket",
                        {"ticket": arguments.model_dump(mode="json")},
                        tool_call_id=call_id,
                    )
                ]
            )
        return ModelResponse(parts=[TextPart("Sandbox ticket confirmed")])

    result = await agent.run(
        "Create the explicitly requested sandbox ticket" if messages is None else None,
        message_history=history,
        deferred_tool_results=DeferredToolResults(approvals={call_id: True}) if messages else None,
        model=FunctionModel(request),
        usage_limits=UsageLimits(request_limit=1),
    )
    if isinstance(result.output, DeferredToolRequests):
        approvals = result.output.approvals
        if (
            messages is not None
            or len(approvals) != 1
            or result.output.calls
            or approvals[0].tool_call_id != call_id
            or approvals[0].tool_name != "create_ticket"
            or approvals[0].args_as_dict() != {"ticket": arguments.model_dump(mode="json")}
        ):
            raise ApprovalError("APPROVAL_BINDING_MISMATCH")
        return approvals[0].args_as_json_str(), result.all_messages_json().decode()
    if messages is None:
        raise ApprovalError("APPROVAL_REQUIRED")
    return result.output, None
