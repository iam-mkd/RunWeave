"""Seeded fault profiles for the LLM proxy.

A profile lists faults with probabilities. For each proxied call, one uniform draw derived
from the seed and the call's position in the run attempt picks at most one applicable
fault, so the same seed and the same sequence of calls reproduce the same faults.
"""

import hashlib
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from runweave.proxy.mock import Api, ModelRequest

FaultKind = Literal[
    "rate_limit",
    "server_error",
    "timeout",
    "slow_first_token",
    "stream_disconnect",
    "malformed_tool_call",
    "unknown_tool",
    "context_length",
]
# Faults that answer with an error instead of a model reply; they cost no budget.
ERROR_FAULTS = frozenset({"rate_limit", "server_error", "timeout", "context_length"})
CUSTOM_PROFILE = "custom"


class FaultSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: FaultKind
    probability: float = Field(ge=0, le=1)
    apis: tuple[Api, ...] | None = Field(default=None, min_length=1)
    status: int = Field(default=503, ge=500, le=599, description="server_error status")
    retry_after: int = Field(default=1, ge=0, le=600, description="rate_limit Retry-After")
    seconds: float = Field(
        default=5, ge=0, le=600, description="timeout hold, or slow_first_token delay"
    )
    after_chunks: int = Field(default=2, ge=0, le=10000, description="stream_disconnect cut")

    def applies_to(self, request: ModelRequest) -> bool:
        if self.apis is not None and request.api not in self.apis:
            return False
        if self.kind == "stream_disconnect":
            return request.stream
        if self.kind in {"malformed_tool_call", "unknown_tool"}:
            return bool(request.tool_names)
        return True


class FaultProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(pattern=r"^[a-z][a-z0-9-]{0,39}$")
    description: str = Field(default="", max_length=300)
    faults: tuple[FaultSpec, ...] = Field(min_length=1, max_length=16)

    @model_validator(mode="after")
    def total_probability(self) -> Self:
        if sum(fault.probability for fault in self.faults) > 1 + 1e-9:
            raise ValueError("Fault probabilities in a profile add up to at most 1")
        return self


def _profile(name: str, description: str, *faults: dict[str, object]) -> FaultProfile:
    return FaultProfile.model_validate({"name": name, "description": description, "faults": faults})


BUILT_IN_PROFILES: dict[str, FaultProfile] = {
    p.name: p
    for p in (
        _profile(
            "flaky",
            "Occasional rate limits, 503s and slow first tokens.",
            {"kind": "rate_limit", "probability": 0.1, "retry_after": 1},
            {"kind": "server_error", "probability": 0.05, "status": 503},
            {"kind": "slow_first_token", "probability": 0.05, "seconds": 2},
        ),
        _profile(
            "rate-limited",
            "Three in ten calls are rate limited.",
            {"kind": "rate_limit", "probability": 0.3, "retry_after": 1},
        ),
        _profile(
            "outage", "Every call fails with 503.", {"kind": "server_error", "probability": 1}
        ),
        _profile(
            "slow",
            "Slow first tokens and some timeouts.",
            {"kind": "slow_first_token", "probability": 0.5, "seconds": 3},
            {"kind": "timeout", "probability": 0.1, "seconds": 60},
        ),
        _profile(
            "broken-tools",
            "Malformed tool-call JSON and unknown tool names.",
            {"kind": "malformed_tool_call", "probability": 0.3},
            {"kind": "unknown_tool", "probability": 0.2},
        ),
        _profile(
            "chaos",
            "A little of everything.",
            {"kind": "rate_limit", "probability": 0.1},
            {"kind": "server_error", "probability": 0.08, "status": 502},
            {"kind": "timeout", "probability": 0.04, "seconds": 30},
            {"kind": "slow_first_token", "probability": 0.08, "seconds": 2},
            {"kind": "stream_disconnect", "probability": 0.08},
            {"kind": "malformed_tool_call", "probability": 0.08},
            {"kind": "unknown_tool", "probability": 0.06},
            {"kind": "context_length", "probability": 0.04},
        ),
    )
}


class _ProfileFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1]
    profiles: list[FaultProfile] = Field(max_length=100)


def load_profiles(path: Path | None) -> dict[str, FaultProfile]:
    profiles = dict(BUILT_IN_PROFILES)
    if path is None:
        return profiles
    try:
        data = path.read_bytes()
        if len(data) > 1_048_576:
            raise ValueError("larger than 1 MiB")
        loaded = _ProfileFile.model_validate_json(data).profiles
    except (OSError, ValueError, ValidationError) as error:
        detail = type(error).__name__ if isinstance(error, ValidationError) else str(error)
        raise ValueError(f"Invalid fault profile file {path.name}: {detail}") from None
    for profile in loaded:
        if profile.name in profiles or profile.name == CUSTOM_PROFILE:
            raise ValueError(f"Fault profile {profile.name!r} clashes with a built-in name")
        profiles[profile.name] = profile
    return profiles


class FaultPlan:
    def __init__(self, profile: FaultProfile | None, seed: int) -> None:
        self.profile = profile
        self._seed = seed

    def _draw(self, call: int) -> float:
        digest = hashlib.sha256(f"runweave-faults:{self._seed}:{call}".encode()).digest()
        return int.from_bytes(digest[:8], "big") / 2**64

    def choose(self, call: int, request: ModelRequest) -> FaultSpec | None:
        if self.profile is None:
            return None
        draw = self._draw(call)
        threshold = 0.0
        for fault in self.profile.faults:
            if not fault.applies_to(request):
                continue
            threshold += fault.probability
            if draw < threshold:
                return fault
        return None
