import asyncio
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, model_validator

from runweave.agents.contracts import Evidence, ToolCall
from runweave.checkpoints import CheckpointError, CheckpointStore, operation_identity


class EffectConfirmation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["committed", "absent", "unknown"]
    evidence: Evidence | None = None

    @model_validator(mode="after")
    def validate_confirmation(self) -> "EffectConfirmation":
        if (self.status == "committed") != (self.evidence is not None):
            raise ValueError("Committed effects require confirmed evidence")
        return self


class EffectAdapter(Protocol):
    idempotent: bool

    async def perform(self, operation_id: str) -> Evidence: ...
    async def reconcile(self, operation_id: str) -> EffectConfirmation: ...


class EffectRunner:
    """Internal boundary for explicitly wired effects; public triage tools are read-only."""

    def __init__(self, store: CheckpointStore, *, timeout: float = 5) -> None:
        self._store = store
        self._timeout = timeout

    async def _confirm(self, adapter: EffectAdapter, operation_id: str) -> EffectConfirmation:
        try:
            async with asyncio.timeout(self._timeout):
                return await adapter.reconcile(operation_id)
        except Exception:
            return EffectConfirmation(status="unknown")

    async def run(self, call: ToolCall, adapter: EffectAdapter) -> Evidence:
        key, digest = operation_identity(
            self._store.run_id, "effect", call.name, call.id, call.arguments
        )
        record, fresh = await self._store.prepare_operation(key, digest, "effect")
        if record.status == "committed" and record.evidence is not None:
            return record.evidence
        if record.status == "needs_resolution":
            raise CheckpointError("EFFECT_RECONCILIATION_REQUIRED")
        if not fresh:
            confirmed = await self._confirm(adapter, key)
            if confirmed.status == "committed" and confirmed.evidence is not None:
                await self._store.commit_operation(key, confirmed.evidence)
                return confirmed.evidence
            if confirmed.status != "absent" or not adapter.idempotent:
                await self._store.require_resolution(key)
                raise CheckpointError("EFFECT_RECONCILIATION_REQUIRED")
        try:
            async with asyncio.timeout(self._timeout):
                evidence = await adapter.perform(key)
        except asyncio.CancelledError:
            raise
        except Exception:
            confirmed = await self._confirm(adapter, key)
            if confirmed.status != "committed" or confirmed.evidence is None:
                await self._store.require_resolution(key)
                raise CheckpointError("EFFECT_RECONCILIATION_REQUIRED") from None
            evidence = confirmed.evidence
        if not 1 <= len(evidence.id) <= 100 or len(evidence.content) > 16384:
            await self._store.require_resolution(key)
            raise CheckpointError("EFFECT_RECONCILIATION_REQUIRED")
        await self._store.commit_operation(key, evidence)
        return evidence
