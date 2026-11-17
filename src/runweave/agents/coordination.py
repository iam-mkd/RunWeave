import asyncio
from typing import Literal
from uuid import uuid4

from runweave.agents.budgeted import BudgetedModel
from runweave.agents.context import estimated_tokens, model_payload
from runweave.agents.contracts import (
    AgentModel,
    Evidence,
    IncidentRequest,
    IncidentResult,
    ToolCall,
    ToolExchange,
)
from runweave.agents.handoffs import (
    ROLE_TOOLS,
    HandoffOutput,
    HandoffRequest,
    HandoffState,
    Role,
    RunbookAnalysis,
    current_role,
    role_scope,
)
from runweave.agents.triage import TriageSteps
from runweave.budgets import BudgetExceeded, BudgetPolicy, current_ledger, run_budget
from runweave.checkpoints import (
    CheckpointError,
    CheckpointStore,
    TriageCheckpoint,
    current_checkpoints,
    decode_checkpoint,
    initial_checkpoint,
    replayable_read,
)
from runweave.deadline import STEP_DEADLINE, DeadlineExceeded, deadline_scope
from runweave.security import ContentPolicy, PolicyDenied, current_principal, principal_scope
from runweave.telemetry import Telemetry
from runweave.tools.contracts import HealthObservation
from runweave.tools.registry import ToolRegistry

ROLES: tuple[Role, Role] = ("health", "runbook")


class CoordinationError(CheckpointError):
    pass


class Progress:
    """Per-run state; concurrent branches merge through fenced optimistic checkpoint writes."""

    def __init__(self, checkpoint: TriageCheckpoint, store: CheckpointStore | None) -> None:
        self.checkpoint = checkpoint
        self.store = store
        self.run_id = store.run_id if store else uuid4()
        self.errors: dict[Role, Exception] = {}

    async def load(self) -> TriageCheckpoint:
        return await self.store.load() if self.store else self.checkpoint.model_copy(deep=True)

    async def save(self, checkpoint: TriageCheckpoint) -> TriageCheckpoint:
        if self.store:
            return await self.store.save(checkpoint, checkpoint.sequence)
        if checkpoint.sequence != self.checkpoint.sequence:
            raise CheckpointError("CHECKPOINT_CONFLICT")
        self.checkpoint = decode_checkpoint(
            checkpoint.model_copy(update={"sequence": checkpoint.sequence + 1}).model_dump(
                mode="json"
            )
        )
        return self.checkpoint

    async def branch(
        self, branch: HandoffState, expected: HandoffState | None, *, tokens: int = 0
    ) -> None:
        # Only two writers, each with at most two transitions: five attempts bound CAS contention.
        for _ in range(5):
            saved = await self.load()
            if saved.branches.get(branch.request.role) != expected:
                raise CheckpointError("CHECKPOINT_CONFLICT")
            updated = saved.model_copy(
                update={
                    "branches": {**saved.branches, branch.request.role: branch},
                    "steps_remaining": saved.steps_remaining - 1,
                    "tokens_remaining": saved.tokens_remaining - tokens,
                }
            )
            try:
                await self.save(updated)
                return
            except CheckpointError as exc:
                if exc.code != "CHECKPOINT_CONFLICT":
                    raise
        raise CheckpointError("CHECKPOINT_CONFLICT")


class CoordinatedRunner:
    def __init__(
        self,
        model: AgentModel,
        tools: ToolRegistry,
        telemetry: Telemetry,
        *,
        budget_policy: BudgetPolicy | None = None,
        content_policy: ContentPolicy | None = None,
        max_steps: int = 7,
        completion_budget: int = 2048,
        step_timeout: float = 5,
        concurrency: int = 2,
    ) -> None:
        if concurrency not in (1, 2):
            raise ValueError("Coordination concurrency must be one or two")
        self._policy = budget_policy or BudgetPolicy()
        self._content = content_policy or ContentPolicy()
        self._model = BudgetedModel(model, self._policy, telemetry)
        self._steps = TriageSteps(
            model,
            tools,
            telemetry,
            step_timeout,
            budget_policy=self._policy,
            content_policy=self._content,
        )
        self._tools, self._telemetry = tools, telemetry
        self._max_steps, self._completion_budget = max_steps, completion_budget
        self._timeout, self._concurrency = step_timeout, concurrency

    def _definitions(self, role: Role) -> tuple[dict[str, object], ...]:
        result = []
        for definition in self._tools.definitions():
            function = definition.get("function")
            if isinstance(function, dict) and function.get("name") == ROLE_TOOLS[role]:
                result.append(definition)
        return tuple(result)

    async def _specialist(
        self, incident: IncidentRequest, role: Role, progress: Progress, completion_limit: int
    ) -> None:
        parent = current_principal()
        child = parent.model_copy(update={"scopes": parent.scopes & {f"tools:{ROLE_TOOLS[role]}"}})
        request = HandoffRequest(
            id=f"{progress.run_id}:{role}", role=role, service=incident.service
        )
        saved = (await progress.load()).branches.get(role)
        if saved is not None and saved.request != request:
            raise CoordinationError("HANDOFF_IDENTITY_MISMATCH")
        if saved is not None and saved.stage in ("succeeded", "failed"):
            self._content.check(saved.model_dump_json())
            if saved.output:
                with principal_scope(child):
                    self._tools.validate(saved.output.exchange.call, incident)
            return
        with (
            principal_scope(child),
            role_scope(role),
            self._telemetry.span(f"agent.specialist.{role}", run_id=progress.run_id),
        ):
            if saved is None:
                try:
                    async with deadline_scope(
                        asyncio.get_running_loop().time() + self._timeout, STEP_DEADLINE
                    ):
                        with self._telemetry.step(tool=False):
                            decision = await self._model.decide(
                                incident, (), self._definitions(role), completion_limit
                            )
                        if (
                            not isinstance(decision.action, ToolCall)
                            or decision.action.name != ROLE_TOOLS[role]
                        ):
                            raise ValueError("Unknown or unauthorized tool")
                        proposed = decision.action.model_copy(update={"id": f"handoff-{role}"})
                        self._content.check(proposed.model_dump_json())
                        self._tools.validate(proposed, incident)
                except Exception as exc:
                    await self._fail(progress, request, saved, exc)
                    return
                new = HandoffState(request=request, stage="collect", call=proposed)
                await progress.branch(new, None, tokens=decision.completion_tokens)
                saved = new
            call = saved.call
            if call is None:
                raise CoordinationError("HANDOFF_INVALID")
            try:
                self._tools.validate(call, incident)

                async def execute() -> Evidence:
                    evidence = await self._tools.execute(call, incident)
                    self._content.check(evidence.id)
                    self._content.check(evidence.content)
                    HandoffOutput(
                        request=request,
                        exchange=ToolExchange(call, evidence),
                        analysis=HealthObservation.model_validate_json(
                            evidence.content, strict=True
                        )
                        if role == "health"
                        else RunbookAnalysis(excerpt=evidence.content[:512]),
                    )
                    return evidence

                async with deadline_scope(
                    asyncio.get_running_loop().time() + self._timeout, STEP_DEADLINE
                ):
                    with self._telemetry.step(tool=True):
                        evidence = await replayable_read(call, execute)
                output = HandoffOutput(
                    request=request,
                    exchange=ToolExchange(call, evidence),
                    analysis=HealthObservation.model_validate_json(evidence.content, strict=True)
                    if role == "health"
                    else RunbookAnalysis(excerpt=evidence.content[:512]),
                )
                self._content.check(output.model_dump_json())
            except Exception as exc:
                await self._fail(progress, request, saved, exc)
                return
            await progress.branch(
                HandoffState(request=request, stage="succeeded", call=call, output=output), saved
            )
            self._telemetry.audit("handoff", "committed", parent)

    async def _fail(
        self,
        progress: Progress,
        request: HandoffRequest,
        saved: HandoffState | None,
        exc: Exception,
    ) -> None:
        code: Literal[
            "HANDOFF_FAILED", "HANDOFF_DENIED", "RUN_BUDGET_EXCEEDED", "STEP_DEADLINE_EXCEEDED"
        ] = (
            "RUN_BUDGET_EXCEEDED"
            if isinstance(exc, BudgetExceeded)
            else "STEP_DEADLINE_EXCEEDED"
            if isinstance(exc, DeadlineExceeded)
            else "HANDOFF_DENIED"
            if isinstance(exc, (PolicyDenied, ValueError))
            else "HANDOFF_FAILED"
        )
        failure = HandoffState(
            request=request, stage="failed", call=saved.call if saved else None, error_code=code
        )
        await progress.branch(failure, saved)
        progress.errors[request.role] = exc
        self._telemetry.audit("handoff", "failed")

    async def run(self, incident: IncidentRequest) -> IncidentResult:
        if current_role() is not None:
            raise CoordinationError("DELEGATION_DENIED")
        parent = current_principal()
        parent.require("agents:delegate")
        for role in ROLES:
            parent.require(f"tools:{ROLE_TOOLS[role]}")
        self._content.check(incident.model_dump_json())
        try:
            with run_budget(self._policy):
                return await self._run(incident)
        except ExceptionGroup as group:
            error: Exception = group
            while isinstance(error, ExceptionGroup) and len(error.exceptions) == 1:
                error = error.exceptions[0]
            if error is group:
                raise
            raise error from None

    async def _run(self, incident: IncidentRequest) -> IncidentResult:
        state = await self._steps.restore(
            incident, self._max_steps, self._completion_budget, "coordinated"
        )
        initial = state.checkpoint or initial_checkpoint(
            incident, "coordinated", self._max_steps, (await current_ledger().read()).policy
        )
        progress = Progress(initial, current_checkpoints())
        for branch in initial.branches.values():
            if branch.request.id != f"{progress.run_id}:{branch.request.role}":
                raise CoordinationError("HANDOFF_IDENTITY_MISMATCH")
            if branch.stage == "failed":
                raise CoordinationError(branch.error_code or "HANDOFF_FAILED")
        if initial.next_node in ("finish", "done"):
            if not isinstance(initial.action, IncidentResult):
                raise CoordinationError("HANDOFF_INVALID")
            return await self._steps.finish(state, initial.action)
        pending = [
            role
            for role in ROLES
            if role not in initial.branches or initial.branches[role].stage == "collect"
        ]
        required_steps = 1 + sum(2 if role not in initial.branches else 1 for role in pending)
        budget = await current_ledger().read()
        model_calls = 1 + sum(role not in initial.branches for role in pending)
        if (
            initial.steps_remaining < required_steps
            or initial.tokens_remaining < model_calls
            or budget.requests + model_calls > budget.policy.max_requests
            or budget.policy.max_output_tokens - budget.output_tokens < model_calls
        ):
            raise BudgetExceeded()
        child_incident = incident.model_copy(update={"memory": None, "create_ticket": False})
        completion_limit = max(1, min(512, initial.tokens_remaining // model_calls))
        inputs = 0
        for role in pending:
            with role_scope(role):
                inputs += estimated_tokens(
                    model_payload(
                        child_incident, (), self._definitions(role), budget.policy.model, 512
                    )
                )
        parallel = (
            self._concurrency == 2
            and len(pending) == 2
            and budget.policy.max_output_tokens - budget.output_tokens >= 1025
            and budget.input_tokens + inputs <= budget.policy.max_input_tokens
            and budget.policy.cost(budget.input_tokens + inputs, budget.output_tokens + 1024)
            <= budget.policy.max_cost_nanos
        )
        self._telemetry.audit("coordination", "parallel" if parallel else "sequential")
        if parallel:
            async with asyncio.TaskGroup() as group:
                for role in ROLES:
                    group.create_task(
                        self._specialist(child_incident, role, progress, completion_limit),
                        name=f"specialist:{role}",
                    )
        else:
            for role in ROLES:
                await self._specialist(child_incident, role, progress, completion_limit)
        saved = await progress.load()
        for role in ROLES:
            branch = saved.branches[role]
            if branch.stage != "succeeded" or branch.output is None:
                raise CoordinationError(
                    branch.error_code or "HANDOFF_FAILED"
                ) from progress.errors.get(role)
            self._content.check(branch.model_dump_json())
        exchanges = [saved.branches[role].output.exchange for role in ROLES]  # type: ignore[union-attr]
        if saved.history != exchanges:
            saved = await progress.save(saved.model_copy(update={"history": exchanges}))
        with (
            role_scope("coordinator"),
            self._telemetry.span("agent.coordinator", run_id=progress.run_id),
        ):
            with self._telemetry.step(tool=False):
                async with deadline_scope(
                    asyncio.get_running_loop().time() + self._timeout, STEP_DEADLINE
                ):
                    decision = await self._model.decide(
                        incident, tuple(exchanges), (), saved.tokens_remaining
                    )
            if not isinstance(decision.action, IncidentResult):
                raise CoordinationError("DELEGATION_DENIED")
            self._content.check(decision.action.model_dump_json())
            saved = await progress.save(
                saved.model_copy(
                    update={
                        "action": decision.action,
                        "next_node": "finish",
                        "steps_remaining": saved.steps_remaining - 1,
                        "tokens_remaining": saved.tokens_remaining - decision.completion_tokens,
                    }
                )
            )
        state.history = list(saved.history)
        state.checkpoint = saved if progress.store else None
        state.steps_remaining, state.tokens_remaining = (
            saved.steps_remaining,
            saved.tokens_remaining,
        )
        state.next_node, state.action = saved.next_node, saved.action
        return await self._steps.finish(state, decision.action)
