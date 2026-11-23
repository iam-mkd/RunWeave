"""Agents' effects: side-effecting calls an agent routes through RunWeave with `ctx.effect`.

RunWeave runs the call, records an effect step, and, when the run's fault profile says so,
drops the response after the write committed: the agent sees a timeout although the write
happened. That is the failure per-call fault injection cannot see, and where a retry without
an idempotency key writes twice. `duplicate_writes` finds those in a run's steps.

Steps keep SHA-256 digests of the idempotency key and the resource, never the values.
"""

import hashlib
import json
import re
import time
from collections.abc import Awaitable, Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Protocol, TypeVar

from runweave.proxy.faults import EFFECT_NAME, EffectFaults
from runweave.steps import record_finished_step

__all__ = [
    "EffectFaults",
    "EffectResponseLost",
    "duplicate_writes",
    "effect_faults_scope",
    "operation_digest",
    "resource_digest",
]

T = TypeVar("T")
_NAME = re.compile(EFFECT_NAME)


class EffectResponseLost(TimeoutError):
    """The effect's write was committed, but its response was lost (an injected fault).

    A `TimeoutError`, as a read timeout after sending a request looks to an HTTP client.
    Retrying safely needs an idempotency key the downstream honours.
    """

    code = "EFFECT_RESPONSE_LOST"

    def __init__(self, effect: str) -> None:
        super().__init__(f"The response of effect {effect!r} was lost after the write")
        self.effect = effect


class EffectFaultSource(Protocol):
    def lost(self, position: int, name: str) -> bool: ...


_faults: ContextVar[EffectFaultSource | None] = ContextVar("runweave_effect_faults", default=None)


@contextmanager
def effect_faults_scope(faults: EffectFaultSource | None) -> Iterator[None]:
    token = _faults.set(faults)
    try:
        yield
    finally:
        _faults.reset(token)


def current_effect_faults() -> EffectFaultSource | None:
    return _faults.get()


def _digest(kind: str, value: str) -> str:
    return hashlib.sha256(f"runweave-{kind}:{value}".encode()).hexdigest()[:32]


def operation_digest(name: str, key: str | None) -> str:
    """One logical write: its idempotency key, or (without one) the effect itself."""
    return _digest("operation", f"{name}:key:{key}" if key is not None else f"{name}:once")


def resource_digest(value: Any) -> str:
    try:
        # As the TypeScript SDK serialises: sorted keys, no spaces, Unicode kept.
        text = json.dumps(
            value, sort_keys=True, default=str, separators=(",", ":"), ensure_ascii=False
        )
    except (TypeError, ValueError):
        text = repr(value)
    return _digest("resource", text)


def check_name(name: str) -> str:
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        raise ValueError(
            f"Effect name {name!r} must be 1-64 lowercase letters, digits, '_', '.' or '-'"
        )
    return name


async def run_effect(
    name: str,
    call: Callable[[], Awaitable[T]],
    *,
    position: int,
    key: str | None,
    resource: Callable[[T], Any] | None,
) -> T:
    """Run one effect call, record its step, and lose its response if the plan says so."""
    operation = operation_digest(name, key)
    faults = current_effect_faults()
    started = time.perf_counter()
    try:
        result = await call()
    except BaseException as error:
        await record_finished_step(
            name, f"call failed: {type(error).__name__}", "failed",
            (time.perf_counter() - started) * 1000,
            kind="effect", effect=name, operation=operation,
        )  # fmt: skip
        raise
    produced = resource_digest(resource(result) if resource is not None else result)
    lost = faults is not None and faults.lost(position, name)
    await record_finished_step(
        name,
        "write committed · response lost" if lost else "write committed",
        "failed" if lost else "succeeded",
        (time.perf_counter() - started) * 1000,
        kind="effect", effect=name, operation=operation, resource=produced,
        fault="response_lost" if lost else None,
    )  # fmt: skip
    if lost:
        raise EffectResponseLost(name)
    return result


def duplicate_writes(
    steps: Iterable[Mapping[str, Any]], effects: Iterable[str] | None = None
) -> list[str]:
    """Effects in a run where one operation produced more than one resource, sorted.

    Without an idempotency key, all calls of an effect are one operation: an agent that
    legitimately writes several times passes a distinct key for each write.
    """
    wanted = None if effects is None else set(effects)
    produced: dict[tuple[str, str], set[str]] = {}
    for step in steps:
        name, resource = step.get("effect"), step.get("resource")
        if step.get("kind") != "effect" or not name or not resource:
            continue
        if wanted is not None and name not in wanted:
            continue
        produced.setdefault((name, str(step.get("operation"))), set()).add(resource)
    return sorted({name for (name, _), resources in produced.items() if len(resources) > 1})
