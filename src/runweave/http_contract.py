"""The versioned HTTP contract between RunWeave and agents it calls (`runweave.agent/v1`).

Both sides use these models: RunWeave's HTTP adapter and the agent-side SDKs. The
published OpenAPI document (contracts/agent-http-v1.openapi.json) is generated from them.
Unknown fields are ignored so minor versions can add optional fields.
"""

from typing import Any, Final, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

CONTRACT_VERSION: Final = "runweave.agent/v1"
MANIFEST_PATH = "/runweave/v1/agents"
NAME_PATTERN = r"^[a-z][a-z0-9-]{0,62}$"
VERSION_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9.+_-]{0,39}$"
CODE_PATTERN = r"^[A-Z][A-Z0-9_]{0,63}$"
MAX_AGENTS = 16
MAX_DEADLINE_MS = 86_400_000


def run_path(name: str) -> str:
    return f"{MANIFEST_PATH}/{name}/runs"


def cancel_path(name: str, run_id: UUID) -> str:
    return f"{run_path(name)}/{run_id}/cancel"


class _Message(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class AgentDescriptor(_Message):
    name: str = Field(pattern=NAME_PATTERN)
    version: str = Field(pattern=VERSION_PATTERN)
    description: str = Field(default="", max_length=500)
    input_schema: dict[str, Any] = Field(description="JSON Schema (2020-12) for run input")
    output_schema: dict[str, Any] = Field(description="JSON Schema (2020-12) for run output")
    max_steps: int = Field(default=50, ge=1, le=200)


class Manifest(_Message):
    contract: Literal["runweave.agent/v1"]
    agents: list[AgentDescriptor] = Field(min_length=1, max_length=MAX_AGENTS)


class ProxyBinding(_Message):
    """The run's LLM proxy binding: use `token` as the API key with these base URLs."""

    token: str = Field(pattern=r"^rwp_[A-Za-z0-9_-]{20,200}$")
    openai_base_url: str = Field(max_length=2048)
    anthropic_base_url: str = Field(max_length=2048)


class RunRequest(_Message):
    run_id: UUID
    attempt: int = Field(ge=1, le=1000, description="2+ when RunWeave re-runs after lease loss")
    input: dict[str, Any]
    deadline_ms: int = Field(
        ge=1, le=MAX_DEADLINE_MS, description="Milliseconds left in the run's deadline"
    )
    proxy: ProxyBinding | None = Field(
        default=None, description="Present when RunWeave's LLM proxy is available to the run"
    )
    approvals_url: str | None = Field(
        default=None,
        max_length=2048,
        description="Where the agent requests approvals (POST, with the proxy token as a "
        "bearer token); present only in durable mode",
    )


class StepRecord(_Message):
    title: str = Field(min_length=1, max_length=120)
    detail: str | None = Field(default=None, max_length=1000)
    status: Literal["succeeded", "failed"]
    duration_ms: float = Field(ge=0, le=MAX_DEADLINE_MS)


class RunResponse(_Message):
    output: dict[str, Any]
    steps: list[StepRecord] = Field(default_factory=list, max_length=200)


class ErrorDetail(_Message):
    code: str = Field(pattern=CODE_PATTERN)
    errors: list[dict[str, Any]] | None = Field(default=None, max_length=20)


class ErrorResponse(_Message):
    error: ErrorDetail


# Codes an agent-side SDK may report in a 500 response that RunWeave passes through.
REPORTED_FAILURES = frozenset(
    {
        "RUN_FAILED",
        "AGENT_STEP_LIMIT",
        "AGENT_OUTPUT_INVALID",
        "AGENT_DEADLINE_EXCEEDED",
        "APPROVAL_EXPIRED",
        "APPROVAL_CANCELLED",
        "APPROVAL_CAPACITY_EXCEEDED",
        "DURABLE_APPROVAL_REQUIRED",
    }
)


def openapi_document() -> dict[str, Any]:
    models: list[type[BaseModel]] = [
        AgentDescriptor,
        Manifest,
        ProxyBinding,
        RunRequest,
        StepRecord,
        RunResponse,
        ErrorDetail,
        ErrorResponse,
    ]
    schemas: dict[str, Any] = {}
    for model in models:
        schema = model.model_json_schema(ref_template="#/components/schemas/{model}")
        schemas.update(schema.pop("$defs", {}))
        schemas[model.__name__] = schema

    def ref(name: str) -> dict[str, str]:
        return {"$ref": f"#/components/schemas/{name}"}

    def json_body(name: str, description: str) -> dict[str, Any]:
        return {"description": description, "content": {"application/json": {"schema": ref(name)}}}

    error = json_body("ErrorResponse", "Contract error with a machine-readable code")
    name = {
        "name": "name",
        "in": "path",
        "required": True,
        "schema": {"type": "string", "pattern": NAME_PATTERN},
    }
    run_id = {
        "name": "run_id",
        "in": "path",
        "required": True,
        "schema": {"type": "string", "format": "uuid"},
    }
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "RunWeave HTTP agent contract",
            "version": CONTRACT_VERSION,
            "description": (
                "Endpoints an HTTP agent serves so RunWeave can discover, run and cancel it. "
                "RunWeave never follows redirects, bounds response sizes, retries only "
                "connection failures and 429/502/503/504 (with the same Idempotency-Key), "
                "and closes the request when the run is cancelled or its deadline passes."
            ),
        },
        "paths": {
            MANIFEST_PATH: {
                "get": {
                    "operationId": "listAgents",
                    "summary": "Describe the agents served at this base URL",
                    "responses": {"200": json_body("Manifest", "Agent manifest")},
                }
            },
            "/runweave/v1/agents/{name}/runs": {
                "post": {
                    "operationId": "runAgent",
                    "summary": "Run the agent once and return its validated output",
                    "parameters": [
                        name,
                        {
                            "name": "Idempotency-Key",
                            "in": "header",
                            "required": False,
                            "description": "The RunWeave run ID. Repeats of a key return the "
                            "first result instead of running again.",
                            "schema": {"type": "string", "maxLength": 200},
                        },
                    ],
                    "requestBody": {
                        "required": True,
                        "content": {"application/json": {"schema": ref("RunRequest")}},
                    },
                    "responses": {
                        "200": json_body("RunResponse", "Run output and timeline steps"),
                        "400": error,
                        "404": error,
                        "409": error,
                        "413": error,
                        "422": error,
                        "500": json_body(
                            "ErrorResponse",
                            "The agent failed; codes in "
                            + ", ".join(sorted(REPORTED_FAILURES))
                            + " are reported to the run",
                        ),
                        "503": json_body(
                            "ErrorResponse", "Busy; RunWeave retries after Retry-After"
                        ),
                    },
                }
            },
            "/runweave/v1/agents/{name}/runs/{run_id}/cancel": {
                "post": {
                    "operationId": "cancelRun",
                    "summary": "Best-effort cancellation of an in-flight run",
                    "parameters": [name, run_id],
                    "responses": {"202": {"description": "Cancellation requested"}, "404": error},
                }
            },
        },
        "components": {"schemas": schemas},
    }


def write_openapi(path: str) -> None:
    import json
    from pathlib import Path

    text = json.dumps(openapi_document(), indent=2, sort_keys=True) + "\n"
    Path(path).write_text(text, encoding="utf-8", newline="\n")


if __name__ == "__main__":
    import sys

    write_openapi(sys.argv[1] if len(sys.argv) > 1 else "contracts/agent-http-v1.openapi.json")
