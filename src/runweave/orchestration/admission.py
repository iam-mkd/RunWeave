"""Shared admission and output rules for the in-memory and durable schedulers."""

import json
from typing import Any

from pydantic import BaseModel, ValidationError

from runweave.agents.catalog import TRIAGE_AGENT, AgentCatalog, RegisteredAgent, SchemaViolation
from runweave.agents.contracts import IncidentRequest
from runweave.orchestration.models import AgentRunRequest
from runweave.sdk import AgentOutputTooLarge, validation_errors
from runweave.security import ContentPolicy

Submission = AgentRunRequest | IncidentRequest


class AdmissionRejected(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class UnknownAgent(Exception):
    pass


class InvalidRunInput(Exception):
    def __init__(self, errors: list[dict[str, Any]]) -> None:
        self.errors = errors
        super().__init__("Invalid agent input")


def resolve(catalog: AgentCatalog, submission: Submission) -> tuple[RegisteredAgent, BaseModel]:
    if isinstance(submission, IncidentRequest):
        name, payload = TRIAGE_AGENT, submission.model_dump(mode="json")
    else:
        name, payload = submission.agent, submission.input
    definition = catalog.get(name)
    if definition is None:
        raise UnknownAgent()
    try:
        return definition, definition.parse(payload)
    except ValidationError as error:
        raise InvalidRunInput(validation_errors(error)) from None
    except SchemaViolation as error:
        raise InvalidRunInput(error.errors) from None


def input_json(value: BaseModel) -> str:
    return value.model_dump_json()


def output_json(output: BaseModel, *, max_bytes: int, policy: ContentPolicy) -> dict[str, Any]:
    encoded = output.model_dump_json()
    if len(encoded.encode()) > max_bytes:
        raise AgentOutputTooLarge()
    policy.check(encoded)
    result: dict[str, Any] = json.loads(encoded)
    return result
