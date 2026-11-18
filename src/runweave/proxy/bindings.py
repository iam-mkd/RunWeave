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
from uuid import UUID

from runweave.budgets import BudgetLedger
from runweave.sdk import ModelProxy
from runweave.steps import StepLog

TOKEN_PREFIX = "rwp_"


@dataclass
class Binding:
    run_id: UUID
    attempt: int
    agent: str
    ledger: BudgetLedger
    steps: StepLog | None
    expires_at: float
    sequence: Iterator[int] = field(default_factory=count)

    def expired(self) -> bool:
        return asyncio.get_running_loop().time() >= self.expires_at

    def call_id(self, prefix: str) -> str:
        digest = hashlib.sha256(f"{self.run_id}:{self.attempt}:{next(self.sequence)}".encode())
        return f"{prefix}{digest.hexdigest()[:24]}"


def _key(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class ProxyBindings:
    def __init__(self, public_url: str) -> None:
        self._public_url = public_url.rstrip("/")
        self._bindings: dict[str, Binding] = {}

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
    ) -> Iterator[ModelProxy]:
        token = TOKEN_PREFIX + secrets.token_urlsafe(32)
        key = _key(token)
        self._bindings[key] = Binding(run_id, attempt, agent, ledger, steps, expires_at)
        try:
            yield ModelProxy(
                token=token,
                openai_base_url=f"{self._public_url}/llm/v1",
                anthropic_base_url=f"{self._public_url}/llm",
            )
        finally:
            self._bindings.pop(key, None)

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
    )
