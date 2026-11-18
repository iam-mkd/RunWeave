import logging
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import BaseModel, Field

from runweave.agents.catalog import (
    TRIAGE_AGENT,
    AgentCatalog,
    AgentLoadError,
    PythonAgent,
    load_python_agent,
)
from runweave.sdk import AgentSpec, RunContext, agent
from runweave.security import ContentPolicy

SAMPLE = Path(__file__).resolve().parents[1] / "examples" / "sample-agent"


class Question(BaseModel):
    text: str = Field(min_length=1, max_length=100)


class Answer(BaseModel):
    reply: str


@agent(name="echo", version="1.0.0", input=Question, output=Answer, description="Echoes text.")
async def echo(input: Question, ctx: RunContext) -> Answer:
    return Answer(reply=input.text)


@pytest.fixture
def sample_path() -> Iterator[None]:
    sys.path.insert(0, str(SAMPLE))
    try:
        yield
    finally:
        sys.path.remove(str(SAMPLE))
        sys.modules.pop("sample_agent", None)


def test_decorator_attaches_a_validated_spec_with_json_schemas() -> None:
    spec = getattr(echo, "__runweave_agent__")  # noqa: B009
    assert isinstance(spec, AgentSpec)
    assert (spec.name, spec.version, spec.description) == ("echo", "1.0.0", "Echoes text.")
    assert spec.input_schema()["properties"]["text"]["maxLength"] == 100
    assert spec.output_schema()["required"] == ["reply"]


@pytest.mark.parametrize("name", ["Echo", "-echo", "echo agent", "a" * 64, ""])
def test_agent_names_are_restricted(name: str) -> None:
    with pytest.raises(ValueError):
        agent(name=name, version="1", input=Question, output=Answer)


def test_only_async_functions_can_be_agents() -> None:
    def blocking(input: Question, ctx: RunContext) -> Answer:
        return Answer(reply=input.text)

    with pytest.raises(TypeError, match="async def"):
        agent(name="blocking", version="1", input=Question, output=Answer)(cast(Any, blocking))


def test_sample_agent_package_loads_by_module_and_attribute(sample_path: None) -> None:
    loaded = load_python_agent("sample_agent:text_report")
    assert loaded.info.name == "text-report"
    assert loaded.info.kind == "python"
    assert loaded.info.target == "sample_agent:text_report"
    assert "text" in loaded.info.input_schema["properties"]


@pytest.mark.parametrize(
    ("target", "message"),
    [
        ("sample_agent", "module:attribute"),
        ("../sample_agent:text_report", "module:attribute"),
        ("sample_agent:text_report()", "module:attribute"),
        ("runweave_missing_module:run", "could not be imported"),
        ("json:dumps", "not decorated"),
        ("sample_agent:missing", "has no attribute"),
    ],
)
def test_invalid_targets_fail_with_clear_messages(
    sample_path: None, target: str, message: str
) -> None:
    with pytest.raises(AgentLoadError, match=message):
        load_python_agent(target)


def test_catalog_requires_unique_names_and_keeps_the_built_in_default() -> None:
    first = PythonAgent.from_function(echo, target="tests.test_sdk:echo")
    catalog = AgentCatalog([first])
    assert catalog.get("echo") is first
    assert catalog.get("unknown") is None
    with pytest.raises(AgentLoadError, match="more than once"):
        AgentCatalog([first, PythonAgent.from_function(echo, target="other:echo")])

    @agent(name=TRIAGE_AGENT, version="1", input=Question, output=Answer)
    async def impostor(input: Question, ctx: RunContext) -> Answer:
        return Answer(reply="")

    with pytest.raises(AgentLoadError, match="reserved"):
        AgentCatalog(
            [PythonAgent.from_function(impostor, target="x:impostor")], reserved=(TRIAGE_AGENT,)
        )


def test_content_policy_redacts_configured_secrets_in_any_encoding() -> None:
    policy = ContentPolicy(("s3cr3t-value",))
    assert (
        policy.redact('token=s3cr3t-value and "s3cr3t-value"')
        == 'token=[REDACTED] and "[REDACTED]"'
    )
    assert policy.redact("nothing here") == "nothing here"


def test_run_context_logs_are_redacted_and_bounded(caplog: pytest.LogCaptureFixture) -> None:
    context = RunContext.detached(agent="echo", content_policy=ContentPolicy(("hunter2",)))
    with caplog.at_level(logging.INFO, logger="runweave.agent"):
        context.log("password is hunter2 " + "x" * 5000)
    record = caplog.records[-1]
    assert "hunter2" not in record.getMessage()
    assert "[REDACTED]" in record.getMessage()
    assert len(record.getMessage()) <= 1000
