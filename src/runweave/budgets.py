from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from decimal import Decimal
from typing import Literal, Protocol, TypeVar
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

ChangeResult = TypeVar("ChangeResult")


class BudgetExceeded(ValueError):
    def __init__(self, code: str = "RUN_BUDGET_EXCEEDED") -> None:
        self.code = code
        super().__init__("Run budget exhausted or context exceeds the configured limit")


class BudgetPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    version: Literal["budget-v1"] = "budget-v1"
    model: str = "fake-v1"
    price_version: str = "fake-free-v1"
    estimator: Literal["utf8-bytes-v1"] = "utf8-bytes-v1"
    input_nanos_per_token: int = Field(default=0, ge=0)
    output_nanos_per_token: int = Field(default=0, ge=0)
    max_requests: int = Field(default=20, ge=1, le=1000)
    max_input_tokens: int = Field(default=100000, ge=1)
    max_output_tokens: int = Field(default=2048, ge=1)
    max_cost_nanos: int = Field(default=50000000, ge=0)
    context_tokens: int = Field(default=16384, ge=1)
    evidence_bytes: int = Field(default=2048, ge=128, le=16384)

    def cost(self, input_tokens: int, output_tokens: int) -> int:
        return (
            input_tokens * self.input_nanos_per_token + output_tokens * self.output_nanos_per_token
        )


class CallCharge(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    status: Literal["reserved", "settled", "uncertain"] = "reserved"


class BudgetSummary(BaseModel):
    model_config = ConfigDict(frozen=True)

    model: str
    price_version: str
    estimator: str
    requests: int
    input_tokens: int
    output_tokens: int
    estimated_cost_usd: Decimal
    uncertain_requests: int
    blocked: bool


class BudgetState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    policy: BudgetPolicy
    requests: int = Field(default=0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    calls: dict[str, CallCharge] = Field(default_factory=dict, max_length=1000)
    blocked: bool = False

    def summary(self) -> BudgetSummary:
        return BudgetSummary(
            model=self.policy.model,
            price_version=self.policy.price_version,
            estimator=self.policy.estimator,
            requests=self.requests,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            estimated_cost_usd=Decimal(self.policy.cost(self.input_tokens, self.output_tokens))
            / Decimal(1000000000),
            uncertain_requests=sum(call.status != "settled" for call in self.calls.values()),
            blocked=self.blocked,
        )


class BudgetLedger(Protocol):
    async def read(self) -> BudgetState: ...
    async def update(self, change: Callable[[BudgetState], ChangeResult]) -> ChangeResult: ...


class MemoryBudget:
    def __init__(self, state: BudgetState | None = None) -> None:
        self.state = state or BudgetState(policy=BudgetPolicy())

    async def read(self) -> BudgetState:
        return self.state.model_copy(deep=True)

    async def update(self, change: Callable[[BudgetState], ChangeResult]) -> ChangeResult:
        # No await inside this single-event-loop transaction, including concurrent branches.
        state = self.state.model_copy(deep=True)
        result = change(state)
        self.state = state
        return result


_ledger: ContextVar[BudgetLedger | None] = ContextVar("runweave_budget", default=None)
_reservation: ContextVar["Reservation | None"] = ContextVar("runweave_reservation", default=None)


@contextmanager
def budget_scope(ledger: BudgetLedger) -> Iterator[None]:
    token = _ledger.set(ledger)
    try:
        yield
    finally:
        _ledger.reset(token)


@contextmanager
def run_budget(policy: BudgetPolicy) -> Iterator[BudgetLedger]:
    ledger = _ledger.get() or MemoryBudget(BudgetState(policy=policy))
    with budget_scope(ledger):
        yield ledger


def current_ledger() -> BudgetLedger:
    ledger = _ledger.get()
    if ledger is None:
        raise RuntimeError("A run budget must be installed before model dispatch")
    return ledger


def current_reservation() -> "Reservation | None":
    return _reservation.get()


class Reservation:
    def __init__(self, ledger: BudgetLedger, call_id: str, output_limit: int) -> None:
        self.ledger = ledger
        self.call_id = call_id
        self.output_limit = output_limit
        self.reconciled = False

    @classmethod
    async def reserve(
        cls, ledger: BudgetLedger, model: str, input_tokens: int, output_tokens: int
    ) -> "Reservation":
        call_id = uuid4().hex

        def charge(state: BudgetState) -> int:
            policy = state.policy
            if state.blocked:
                raise BudgetExceeded("PROVIDER_USAGE_EXCEEDED_RESERVATION")
            if policy.model != model:
                raise BudgetExceeded("BUDGET_POLICY_MISMATCH")
            output_limit = min(output_tokens, policy.max_output_tokens - state.output_tokens)
            if input_tokens + output_limit > policy.context_tokens:
                raise BudgetExceeded("CONTEXT_LIMIT_EXCEEDED")
            if (
                output_limit <= 0
                or state.requests >= policy.max_requests
                or state.input_tokens + input_tokens > policy.max_input_tokens
                or policy.cost(
                    state.input_tokens + input_tokens, state.output_tokens + output_limit
                )
                > policy.max_cost_nanos
            ):
                raise BudgetExceeded()
            state.requests += 1
            state.input_tokens += input_tokens
            state.output_tokens += output_limit
            state.calls[call_id] = CallCharge(input_tokens=input_tokens, output_tokens=output_limit)
            return output_limit

        limit = await ledger.update(charge)
        return cls(ledger, call_id, limit)

    @contextmanager
    def installed(self) -> Iterator[None]:
        token = _reservation.set(self)
        try:
            yield
        finally:
            _reservation.reset(token)

    async def settle(self, input_tokens: int | None, output_tokens: int | None) -> None:
        if self.reconciled:
            return
        if any(
            value is not None and (type(value) is not int or value < 0)
            for value in (input_tokens, output_tokens)
        ):
            raise BudgetExceeded("PROVIDER_USAGE_INVALID")

        def reconcile(state: BudgetState) -> bool:
            call = state.calls[self.call_id]
            if call.status == "settled":
                return False
            actual_input = call.input_tokens if input_tokens is None else input_tokens
            actual_output = call.output_tokens if output_tokens is None else output_tokens
            if actual_input < 0 or actual_output < 0:
                raise BudgetExceeded("PROVIDER_USAGE_INVALID")
            exceeded = actual_input > call.input_tokens or actual_output > call.output_tokens
            state.blocked = state.blocked or exceeded
            state.input_tokens += actual_input - call.input_tokens
            state.output_tokens += actual_output - call.output_tokens
            call.input_tokens = actual_input
            call.output_tokens = actual_output
            call.status = (
                "uncertain" if input_tokens is None or output_tokens is None else "settled"
            )
            return exceeded

        exceeded = await self.ledger.update(reconcile)
        self.reconciled = True
        if exceeded:
            raise BudgetExceeded("PROVIDER_USAGE_EXCEEDED_RESERVATION")
