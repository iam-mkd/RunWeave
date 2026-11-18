"""Public Python SDK for agents that RunWeave runs and tests.

from runweave.sdk import RunContext, agent

@agent(name="my-agent", version="1.0.0", input=MyInput, output=MyOutput)
async def run(input: MyInput, ctx: RunContext) -> MyOutput: ...
"""

import inspect
import logging
import re
from collections.abc import Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Any, TypeVar
from uuid import UUID

from pydantic import BaseModel, ValidationError

from runweave.budgets import BudgetSummary, current_ledger
from runweave.deadline import remaining_budget
from runweave.security import ContentPolicy
from runweave.steps import (
    DETAIL_LIMIT,
    TITLE_LIMIT,
    StepStatus,
    clip,
    record_finished_step,
    record_step,
)

NAME_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
VERSION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+_-]{0,39}$")
LOG_LIMIT = 1000
_logger = logging.getLogger("runweave.agent")


class AgentError(ValueError):
    """A classified agent failure whose code is safe to show in run snapshots."""

    code = "RUN_FAILED"

    def __init__(self) -> None:
        super().__init__(self.code)


class StepLimitExceeded(AgentError):
    code = "AGENT_STEP_LIMIT"


class AgentOutputInvalid(AgentError):
    code = "AGENT_OUTPUT_INVALID"


class AgentOutputTooLarge(AgentError):
    code = "AGENT_OUTPUT_TOO_LARGE"


class AgentUnavailable(AgentError):
    code = "AGENT_UNAVAILABLE"


def validation_errors(error: ValidationError, limit: int = 20) -> list[dict[str, Any]]:
    """Locations, types and messages describe the schema; submitted values are never echoed."""
    return [
        {"loc": list(item["loc"]), "type": item["type"], "msg": item["msg"]}
        for item in error.errors(include_url=False, include_input=False, include_context=False)[
            :limit
        ]
    ]


@dataclass(frozen=True)
class AgentSpec:
    name: str
    version: str
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    description: str = ""
    max_steps: int = 50

    def input_schema(self) -> dict[str, Any]:
        return self.input_model.model_json_schema()

    def output_schema(self) -> dict[str, Any]:
        return self.output_model.model_json_schema()


AgentFunction = Callable[[Any, "RunContext"], Awaitable[Any]]
F = TypeVar("F", bound=AgentFunction)


def agent(
    *,
    name: str,
    version: str,
    input: type[BaseModel],  # noqa: A002 - matches the decorated function's parameter
    output: type[BaseModel],
    description: str = "",
    max_steps: int = 50,
) -> Callable[[F], F]:
    if not NAME_PATTERN.fullmatch(name):
        raise ValueError("Agent names use lowercase letters, digits and hyphens (max 63)")
    if not VERSION_PATTERN.fullmatch(version):
        raise ValueError("Agent versions are 1-40 characters: letters, digits, . + _ -")
    if len(description) > 500:
        raise ValueError("Agent descriptions are at most 500 characters")
    if not 1 <= max_steps <= 200:
        raise ValueError("max_steps must be between 1 and 200")
    for model in (input, output):
        if not (isinstance(model, type) and issubclass(model, BaseModel)):
            raise TypeError("Agent input and output must be Pydantic models")
    spec = AgentSpec(name, version, input, output, description, max_steps)

    def decorate(function: F) -> F:
        # A stored bool avoids narrowing `function`, which must keep its declared type.
        is_async = inspect.iscoroutinefunction(function)
        if not is_async:
            raise TypeError("RunWeave agents must be declared with async def")
        setattr(function, "__runweave_agent__", spec)  # noqa: B010 - attribute is dynamic
        return function

    return decorate


class RunContext:
    """What a running agent may know and do. Created by RunWeave for each attempt."""

    def __init__(
        self,
        *,
        run_id: UUID | None,
        agent: str,
        attempt: int = 1,
        max_steps: int = 50,
        content_policy: ContentPolicy | None = None,
    ) -> None:
        self.run_id = run_id
        self.agent = agent
        self.attempt = attempt
        self._max_steps = max_steps
        self._steps = 0
        self._policy = content_policy or ContentPolicy()

    @classmethod
    def detached(cls, *, agent: str, content_policy: ContentPolicy | None = None) -> "RunContext":
        """A context for calling an agent directly, for example in its own unit tests."""
        return cls(run_id=None, agent=agent, content_policy=content_policy)

    def remaining_seconds(self) -> float:
        """Seconds left before the run deadline (which includes queue wait and retries)."""
        return max(0.0, remaining_budget())

    async def usage(self) -> BudgetSummary | None:
        try:
            ledger = current_ledger()
        except RuntimeError:
            return None
        return (await ledger.read()).summary()

    def step(self, title: str, detail: str | None = None) -> AbstractAsyncContextManager[None]:
        """Record one timeline step. Exceeding the agent's max_steps fails the run."""
        if self._steps >= self._max_steps:
            raise StepLimitExceeded()
        self._steps += 1
        redacted = self._policy.redact(detail) if detail is not None else None
        return record_step(
            clip(self._policy.redact(title), TITLE_LIMIT) or "Step", clip(redacted, DETAIL_LIMIT)
        )

    async def add_finished_steps(
        self, steps: Sequence[tuple[str, str | None, StepStatus, float]]
    ) -> None:
        """Record steps an agent ran elsewhere (title, detail, status, duration in ms)."""
        if self._steps + len(steps) > self._max_steps:
            raise StepLimitExceeded()
        for title, detail, status, duration_ms in steps:
            self._steps += 1
            await record_finished_step(
                clip(self._policy.redact(title), TITLE_LIMIT) or "Step",
                clip(self._policy.redact(detail), DETAIL_LIMIT) if detail is not None else None,
                status,
                duration_ms,
            )

    def log(self, message: str) -> None:
        text = self._policy.redact(str(message))[:LOG_LIMIT]
        _logger.info(
            text,
            extra={
                "runweave_run_id": str(self.run_id) if self.run_id else None,
                "runweave_agent": self.agent,
            },
        )
