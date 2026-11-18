"""Registered agents: the built-in incident triage adapter and SDK agents loaded at startup.

Agents load only from startup configuration (CLI or settings). Nothing here imports code
because of an HTTP request.
"""

import importlib
import re
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ValidationError

from runweave.agents.contracts import AgentExecutor, IncidentRequest, IncidentResult
from runweave.sdk import AgentFunction, AgentOutputInvalid, AgentSpec, RunContext

TRIAGE_AGENT = "incident-triage"
TARGET_PATTERN = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*:[A-Za-z_][A-Za-z0-9_]*$"
)


class AgentLoadError(ValueError):
    pass


@dataclass(frozen=True)
class AgentInfo:
    name: str
    version: str
    description: str
    kind: Literal["built-in", "python"]
    input_schema: dict[str, Any] = field(repr=False)
    output_schema: dict[str, Any] = field(repr=False)
    max_steps: int
    target: str | None = None


class RegisteredAgent(Protocol):
    @property
    def info(self) -> AgentInfo: ...

    def parse(self, payload: Mapping[str, Any]) -> BaseModel: ...

    async def run(self, input: BaseModel, context: RunContext) -> BaseModel: ...


class PythonAgent:
    def __init__(self, spec: AgentSpec, function: AgentFunction, target: str) -> None:
        self._spec = spec
        self._function = function
        self.info = AgentInfo(
            name=spec.name,
            version=spec.version,
            description=spec.description,
            kind="python",
            input_schema=spec.input_schema(),
            output_schema=spec.output_schema(),
            max_steps=spec.max_steps,
            target=target,
        )

    @classmethod
    def from_function(cls, function: AgentFunction, *, target: str) -> "PythonAgent":
        spec = getattr(function, "__runweave_agent__", None)
        if not isinstance(spec, AgentSpec):
            raise AgentLoadError(f"{target} is not decorated with @runweave.sdk.agent")
        return cls(spec, function, target)

    def parse(self, payload: Mapping[str, Any]) -> BaseModel:
        return self._spec.input_model.model_validate(payload)

    async def run(self, input: BaseModel, context: RunContext) -> BaseModel:
        result = await self._function(input, context)
        if isinstance(result, self._spec.output_model):
            return result
        try:
            return self._spec.output_model.model_validate(result)
        except ValidationError:
            raise AgentOutputInvalid() from None


class TriageAgent:
    """Adapts the existing incident-triage runners; their behavior is unchanged."""

    def __init__(self, executor: AgentExecutor, *, version: str, max_steps: int) -> None:
        self._executor = executor
        self.info = AgentInfo(
            name=TRIAGE_AGENT,
            version=version,
            description="Built-in example: checks synthetic service health and a runbook, "
            "then returns an evidence-backed severity.",
            kind="built-in",
            input_schema=IncidentRequest.model_json_schema(),
            output_schema=IncidentResult.model_json_schema(),
            max_steps=max_steps,
        )

    def parse(self, payload: Mapping[str, Any]) -> BaseModel:
        return IncidentRequest.model_validate(payload)

    async def run(self, input: BaseModel, context: RunContext) -> BaseModel:
        if not isinstance(input, IncidentRequest):
            raise TypeError("The triage agent requires an IncidentRequest")
        return await self._executor.run(input)


class AgentCatalog:
    def __init__(self, agents: Iterable[RegisteredAgent], *, reserved: Sequence[str] = ()) -> None:
        self._agents: dict[str, RegisteredAgent] = {}
        for item in agents:
            name = item.info.name
            if name in self._agents:
                raise AgentLoadError(f"Agent name {name!r} is registered more than once")
            if name in reserved:
                raise AgentLoadError(f"Agent name {name!r} is reserved for the built-in agent")
            self._agents[name] = item
        if not self._agents:
            raise AgentLoadError("At least one agent must be registered")
        self.default = TRIAGE_AGENT if TRIAGE_AGENT in self._agents else next(iter(self._agents))

    @classmethod
    def with_triage(
        cls,
        executor: AgentExecutor,
        *,
        version: str,
        max_steps: int,
        extra: Sequence[RegisteredAgent] = (),
    ) -> "AgentCatalog":
        for item in extra:
            if item.info.name == TRIAGE_AGENT:
                raise AgentLoadError(
                    f"Agent name {TRIAGE_AGENT!r} is reserved for the built-in agent"
                )
        return cls([TriageAgent(executor, version=version, max_steps=max_steps), *extra])

    def get(self, name: str) -> RegisteredAgent | None:
        return self._agents.get(name)

    def __iter__(self) -> Iterator[RegisteredAgent]:
        return iter(self._agents.values())


def load_python_agent(target: str) -> PythonAgent:
    if not TARGET_PATTERN.fullmatch(target):
        raise AgentLoadError(f"Agent target {target!r} must look like module:attribute")
    module_name, attribute = target.split(":")
    try:
        module = importlib.import_module(module_name)
    except Exception as exc:  # noqa: BLE001 - surface any import failure as a clear startup error
        raise AgentLoadError(
            f"Agent module {module_name!r} could not be imported: {type(exc).__name__}: {exc}"[:300]
        ) from None
    if not hasattr(module, attribute):
        raise AgentLoadError(f"Agent module {module_name!r} has no attribute {attribute!r}")
    return PythonAgent.from_function(getattr(module, attribute), target=target)


def load_python_agents(targets: Iterable[str]) -> list[PythonAgent]:
    return [load_python_agent(target) for target in dict.fromkeys(targets)]
