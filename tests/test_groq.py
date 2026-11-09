import json

import httpx
import pytest
from pydantic import ValidationError

from runweave.agents.contracts import IncidentRequest
from runweave.agents.groq import GROQ_URL
from runweave.core.settings import Settings
from runweave.demo import run_demo


def test_groq_requires_replacing_the_placeholder(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RUNWEAVE_MODEL_PROVIDER", "groq")
    monkeypatch.setenv("RUNWEAVE_GROQ_API_KEY", "replace_with_your_groq_api_key")
    with pytest.raises(ValidationError, match="Set RUNWEAVE_GROQ_API_KEY"):
        Settings()


@pytest.mark.asyncio
async def test_groq_runtime_uses_validated_tool_calls_and_preserves_transcript(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RUNWEAVE_MODEL_PROVIDER", "groq")
    monkeypatch.setenv("RUNWEAVE_GROQ_API_KEY", "unit-test-key")
    monkeypatch.setenv("RUNWEAVE_GROQ_REQUESTS_PER_MINUTE", "600")
    monkeypatch.setenv("RUNWEAVE_AGENT_COMPLETION_TOKENS", "64")
    evidence_ids = ["mock-health:payments", "mock-runbook:triage-v1"]
    actions = [
        ("check_health", {"service": "payments"}),
        ("lookup_runbook", {"service": "payments"}),
        (
            "finish_triage",
            {
                "summary": "Synthetic health is degraded. Check recent deployments.",
                "severity": "medium",
                "evidence_ids": evidence_ids,
            },
        ),
    ]
    requests: list[dict[str, object]] = []

    async def respond(self: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        assert str(request.url) == GROQ_URL
        assert request.headers["Authorization"] == "Bearer unit-test-key"
        index = len(requests)
        requests.append(json.loads(await request.aread()))
        name, arguments = actions[index]
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "tool_calls": [
                                {
                                    "id": f"call-{index}",
                                    "type": "function",
                                    "function": {"name": name, "arguments": json.dumps(arguments)},
                                }
                            ]
                        },
                    }
                ],
                "usage": {"completion_tokens": 11},
            },
        )

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", respond)
    result = await run_demo(
        IncidentRequest(service="payments", description="Latency spike"), Settings()
    )
    assert list(result.evidence_ids) == evidence_ids
    assert [request["max_completion_tokens"] for request in requests] == [64, 53, 42]
    messages = requests[-1]["messages"]
    assert isinstance(messages, list)
    assert [message["tool_call_id"] for message in messages if message["role"] == "tool"] == [
        "call-0",
        "call-1",
    ]
    assert json.loads(messages[-1]["content"])["evidence_id"] == evidence_ids[1]
