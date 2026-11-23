"""Per-run proxy tokens. A token is issued when a run attempt starts and revoked when it ends.

Only the SHA-256 of a token is kept, so the table never holds a usable credential.
"""

import asyncio
import hashlib
import secrets
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager, nullcontext
from dataclasses import dataclass, field
from itertools import count
from typing import TYPE_CHECKING
from uuid import UUID

from runweave.agent_effects import effect_faults_scope
from runweave.budgets import BudgetLedger
from runweave.proxy.faults import FaultPlan
from runweave.sdk import ModelProxy
from runweave.steps import StepLog

if TYPE_CHECKING:
    from runweave.storage.action_approvals import PostgresActionGate

TOKEN_PREFIX = "rwp_"
APPROVALS_PATH = "/agent/v1/approvals"


@dataclass
class Binding:
    run_id: UUID
    attempt: int
    agent: str
    ledger: BudgetLedger
    steps: StepLog | None
    expires_at: float
    # The attempt's approval gate (durable mode), reached by HTTP agents' callbacks.
    approvals: "PostgresActionGate | None" = None
    sequence: Iterator[int] = field(default_factory=count)
    # Position of each proxied call in this attempt; seeded faults are drawn from it.
    calls: Iterator[int] = field(default_factory=count)

    def expired(self) -> bool:
        return asyncio.get_running_loop().time() >= self.expires_at

    def call_id(self, prefix: str) -> str:
        digest = hashlib.sha256(f"{self.run_id}:{self.attempt}:{next(self.sequence)}".encode())
        return f"{prefix}{digest.hexdigest()[:24]}"


def _key(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class ProxyBindings:
    def __init__(self, public_url: str, faults: FaultPlan | None = None) -> None:
        self._public_url = public_url.rstrip("/")
        self._bindings: dict[str, Binding] = {}
        # The current fault profile and seed (live settings replace it); each attempt takes
        # its effect faults from it when it starts.
        self.faults = faults

    @property
    def approvals_url(self) -> str:
        return self._public_url + APPROVALS_PATH

    @contextmanager
    def bound(
        self,
        *,
        run_id: UUID,
        attempt: int,
        agent: str,
        ledger: BudgetLedger,
        steps: StepLog | None,
        expires_at: float,
        approvals: "PostgresActionGate | None" = None,
    ) -> Iterator[ModelProxy]:
        token = TOKEN_PREFIX + secrets.token_urlsafe(32)
        key = _key(token)
        self._bindings[key] = Binding(
            run_id, attempt, agent, ledger, steps, expires_at, approvals=approvals
        )
        effect_faults = self.faults.effect_faults() if self.faults is not None else None
        try:
            with effect_faults_scope(effect_faults):
                yield ModelProxy(
                    token=token,
                    openai_base_url=f"{self._public_url}/llm/v1",
                    anthropic_base_url=f"{self._public_url}/llm",
                )
        finally:
            self._bindings.pop(key, None)

    def extend(self, token: str, seconds: float) -> None:
        """Move a binding's expiry with its run deadline (approval waits pause it)."""
        binding = self._bindings.get(_key(token))
        if binding is not None and seconds > 0:
            binding.expires_at += seconds

    def resolve(self, token: str | None) -> Binding | None:
        if not token or not token.startswith(TOKEN_PREFIX) or len(token) > 256:
            return None
        binding = self._bindings.get(_key(token))
        if binding is None or binding.expired():
            return None
        return binding

    def __len__(self) -> int:
        return len(self._bindings)


def bind_run(
    bindings: ProxyBindings | None,
    *,
    run_id: UUID,
    attempt: int,
    agent: str,
    ledger: BudgetLedger,
    steps: StepLog | None,
    expires_at: float,
    approvals: "PostgresActionGate | None" = None,
) -> AbstractContextManager[ModelProxy | None]:
    """The run's proxy binding for the length of one attempt, or None without a proxy."""
    if bindings is None:
        return nullcontext(None)
    return bindings.bound(
        run_id=run_id,
        attempt=attempt,
        agent=agent,
        ledger=ledger,
        steps=steps,
        expires_at=expires_at,
        approvals=approvals,
    )
