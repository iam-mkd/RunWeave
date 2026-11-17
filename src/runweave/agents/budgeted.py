from runweave.agents.context import compact_context
from runweave.agents.contracts import AgentModel, IncidentRequest, ModelDecision, ToolExchange
from runweave.budgets import BudgetExceeded, BudgetPolicy, Reservation, current_ledger
from runweave.memory import MemoryRejected, prepare_recall, recall_scope, refresh_recall
from runweave.security import PolicyDenied
from runweave.telemetry import Telemetry


class BudgetedModel:
    def __init__(self, model: AgentModel, policy: BudgetPolicy, telemetry: Telemetry) -> None:
        self._model = model
        self._policy = policy
        self._telemetry = telemetry

    async def decide(
        self,
        incident: IncidentRequest,
        history: tuple[ToolExchange, ...],
        tools: tuple[dict[str, object], ...],
        completion_budget: int,
    ) -> ModelDecision:
        try:
            recall = await prepare_recall(incident.service, incident.memory)
        except (MemoryRejected, PolicyDenied):
            self._telemetry.audit("memory", "denied")
            raise
        if recall is not None:
            self._telemetry.audit("memory", "recalled" if recall.facts else "empty")
        with recall_scope(recall):
            return await self._decide(incident, history, tools, completion_budget)

    async def _decide(
        self,
        incident: IncidentRequest,
        history: tuple[ToolExchange, ...],
        tools: tuple[dict[str, object], ...],
        completion_budget: int,
    ) -> ModelDecision:
        ledger = current_ledger()
        state = await ledger.read()
        output_limit = min(
            512, completion_budget, state.policy.max_output_tokens - state.output_tokens
        )
        try:
            prepared, input_tokens, compacted = compact_context(
                incident, history, tools, state.policy, output_limit
            )
            reservation = await Reservation.reserve(
                ledger, self._policy.model, input_tokens, output_limit
            )
        except BudgetExceeded:
            self._telemetry.audit("budget", "denied")
            raise
        self._telemetry.audit("budget", "compacted" if compacted else "reserved")
        try:
            with reservation.installed():
                await refresh_recall()
                decision = await self._model.decide(
                    incident, prepared, tools, reservation.output_limit
                )
            if decision.completion_tokens <= 0:
                raise ValueError("Invalid completion token usage")
            if not reservation.reconciled:
                await reservation.settle(decision.input_tokens, decision.completion_tokens)
            return decision
        finally:
            # Retain unsettled charges without an await that could mask cancellation.
            if not reservation.reconciled:
                self._telemetry.audit("budget", "uncertain")
