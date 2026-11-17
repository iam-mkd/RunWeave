import hashlib
import json
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from runweave.agents.context import SYSTEM_PROMPT
from runweave.agents.contracts import (
    Evidence,
    IncidentRequest,
    IncidentResult,
    ToolCall,
    ToolExchange,
)
from runweave.agents.handoffs import COORDINATOR_PROMPT, SPECIALIST_PROMPTS, HandoffState, Role
from runweave.budgets import BudgetPolicy, BudgetState
from runweave.memory import MEMORY_INSTRUCTIONS

Workflow = Literal["baseline", "pydanticai", "coordinated"]
Node = Literal["decide", "collect", "finish", "done"]


class CheckpointError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__("Workflow checkpoint or operation cannot be safely resumed")


class WorkflowIdentity(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    workflow: Workflow
    version: Literal["triage-v1", "coordinated-v1"] = "triage-v1"
    prompt_version: Literal[
        "triage-prompt-v1",
        "triage-memory-prompt-v1",
        "coordinated-prompt-v1",
        "coordinated-memory-prompt-v1",
    ] = "triage-prompt-v1"
    prompt_sha256: str = Field(min_length=64, max_length=64)
    tools_version: Literal["triage-tools-v1"] = "triage-tools-v1"
    model: str = Field(min_length=1, max_length=100)


def workflow_identity(workflow: Workflow, model: str, *, memory: bool = False) -> WorkflowIdentity:
    if workflow == "coordinated":
        return WorkflowIdentity(
            workflow=workflow,
            model=model,
            version="coordinated-v1",
            prompt_version="coordinated-memory-prompt-v1" if memory else "coordinated-prompt-v1",
            prompt_sha256=hashlib.sha256(
                (
                    COORDINATOR_PROMPT
                    + SPECIALIST_PROMPTS["health"]
                    + SPECIALIST_PROMPTS["runbook"]
                    + (MEMORY_INSTRUCTIONS if memory else "")
                ).encode()
            ).hexdigest(),
        )
    return WorkflowIdentity(
        workflow=workflow,
        model=model,
        prompt_version="triage-memory-prompt-v1" if memory else "triage-prompt-v1",
        prompt_sha256=hashlib.sha256(
            (SYSTEM_PROMPT + (MEMORY_INSTRUCTIONS if memory else "")).encode()
        ).hexdigest(),
    )


class TriageCheckpoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal["triage-checkpoint-v1"] = "triage-checkpoint-v1"
    identity: WorkflowIdentity
    sequence: int = Field(default=0, ge=0)
    incident: IncidentRequest
    steps_remaining: int = Field(ge=0, le=20)
    tokens_remaining: int = Field(ge=0, le=8192)
    history: list[ToolExchange] = Field(default_factory=list, max_length=2)
    next_node: Node = "decide"
    action: ToolCall | IncidentResult | None = None
    usage: BudgetState
    branches: dict[Role, HandoffState] = Field(default_factory=dict, max_length=2)

    @model_validator(mode="after")
    def validate_boundary(self) -> "TriageCheckpoint":
        if any(role != branch.request.role for role, branch in self.branches.items()):
            raise ValueError("Invalid branch role")
        if self.branches and self.identity.workflow != "coordinated":
            raise ValueError("Unexpected branch state")
        if self.identity.workflow == "coordinated" and self.next_node in ("finish", "done"):
            if set(self.branches) != {"health", "runbook"} or any(
                branch.stage != "succeeded" or branch.output is None
                for branch in self.branches.values()
            ):
                raise ValueError("Incomplete coordinated result")
            roles: tuple[Role, Role] = ("health", "runbook")
            exchanges = []
            for role in roles:
                output = self.branches[role].output
                if output is None:
                    raise ValueError("Incomplete coordinated result")
                exchanges.append(output.exchange)
            if self.history != exchanges:
                raise ValueError("Inconsistent coordinated evidence")
        kinds: dict[Node, type[object]] = {
            "decide": type(None),
            "collect": ToolCall,
            "finish": IncidentResult,
            "done": IncidentResult,
        }
        expected = kinds[self.next_node]
        if not isinstance(self.action, expected):
            raise ValueError("Invalid checkpoint boundary")
        if any(
            len(item.evidence.content) > 16384 or not 1 <= len(item.evidence.id) <= 100
            for item in self.history
        ):
            raise ValueError("Invalid checkpoint evidence")
        return self


def decode_checkpoint(raw: object) -> TriageCheckpoint:
    if not isinstance(raw, dict) or raw.get("version") != "triage-checkpoint-v1":
        raise CheckpointError("CHECKPOINT_VERSION_UNSUPPORTED")
    try:
        return TriageCheckpoint.model_validate(raw)
    except ValidationError:
        raise CheckpointError("CHECKPOINT_INVALID") from None


def initial_checkpoint(
    incident: IncidentRequest, workflow: Workflow, max_steps: int, policy: BudgetPolicy
) -> TriageCheckpoint:
    return TriageCheckpoint(
        identity=workflow_identity(workflow, policy.model, memory=incident.memory is not None),
        incident=incident,
        steps_remaining=max_steps,
        tokens_remaining=policy.max_output_tokens,
        usage=BudgetState(policy=policy),
    )


class OperationRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=67, max_length=67)
    digest: str = Field(min_length=64, max_length=64)
    kind: Literal["read", "effect"]
    status: Literal["started", "committed", "needs_resolution"] = "started"
    evidence: Evidence | None = None

    @model_validator(mode="after")
    def validate_receipt(self) -> "OperationRecord":
        if (self.status == "committed") != (self.evidence is not None):
            raise ValueError("Invalid operation receipt")
        if self.evidence is not None and (
            not 1 <= len(self.evidence.id) <= 100 or len(self.evidence.content) > 16384
        ):
            raise ValueError("Invalid operation evidence")
        return self


class CheckpointStore(Protocol):
    run_id: UUID

    async def load(self) -> TriageCheckpoint: ...
    async def save(
        self, checkpoint: TriageCheckpoint, expected_sequence: int
    ) -> TriageCheckpoint: ...
    async def prepare_operation(
        self, operation_id: str, digest: str, kind: Literal["read", "effect"]
    ) -> tuple[OperationRecord, bool]: ...
    async def commit_operation(self, operation_id: str, evidence: Evidence) -> None: ...
    async def require_resolution(self, operation_id: str) -> None: ...


_store: ContextVar[CheckpointStore | None] = ContextVar("runweave_checkpoint", default=None)


def current_checkpoints() -> CheckpointStore | None:
    return _store.get()


@contextmanager
def checkpoint_scope(store: CheckpointStore) -> Iterator[None]:
    token = _store.set(store)
    try:
        yield
    finally:
        _store.reset(token)


def operation_identity(
    run_id: UUID, kind: str, name: str, call_id: str, arguments: str
) -> tuple[str, str]:
    canonical = json.dumps(
        {"name": name, "arguments": json.loads(arguments)}, sort_keys=True, separators=(",", ":")
    )
    digest = hashlib.sha256(canonical.encode()).hexdigest()
    key = hashlib.sha256(f"{run_id}/{kind}/{call_id}".encode()).hexdigest()
    return f"rw-{key}", digest


async def replayable_read(call: ToolCall, execute: Callable[[], Awaitable[Evidence]]) -> Evidence:
    store = current_checkpoints()
    if store is None:
        return await execute()
    key, digest = operation_identity(store.run_id, "read", call.name, call.id, call.arguments)
    record, _ = await store.prepare_operation(key, digest, "read")
    if record.status == "committed" and record.evidence is not None:
        return record.evidence
    if record.status == "needs_resolution":
        raise CheckpointError("EFFECT_RECONCILIATION_REQUIRED")
    evidence = await execute()
    await store.commit_operation(key, evidence)
    return evidence
