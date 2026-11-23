"""`runweave.toml`: the project file the testing commands read.

    [agent]
    name = "word-count"           # the agent to test
    python = "agent:run"          # a Python @agent, imported from the project directory
    # http = "http://127.0.0.1:9200"   # or an HTTP agent that is already running
    # command = ["node", "agent.mjs"]  # or one RunWeave starts (it prints RUNWEAVE_AGENT_READY)

    [limits]                      # run_timeout_seconds, concurrency
    [proxy]                       # mode, fault_profile, fault_seed, mock_fixtures
    [[datasets]]                  # name, path (.jsonl, .yaml or .yml)
    [eval]                        # min_pass_rate, baseline, reports
    [bench]                       # concurrency levels, runs, fault_profiles, soak, gates
    [shadow]                      # candidate (an agent table) and promotion thresholds

Paths are relative to the file and must stay inside the project directory.
"""

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

FILENAME = "runweave.toml"
NAME_PATTERN = r"^[a-z][a-z0-9-]{0,62}$"


class ProjectError(ValueError):
    """A project problem worth showing to the developer as-is (no traceback)."""


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class _AgentTarget(_Section):
    python: str | None = Field(
        default=None, pattern=r"^[A-Za-z_][\w.]*:[A-Za-z_]\w*$", max_length=200
    )
    http: str | None = Field(default=None, max_length=2048)
    command: tuple[str, ...] | None = Field(default=None, min_length=1, max_length=32)

    @model_validator(mode="after")
    def one_connection(self) -> "_AgentTarget":
        if self.python is None and self.http is None and self.command is None:
            raise ValueError("set python, http or command to say how to reach the agent")
        if self.python is not None and (self.http is not None or self.command is not None):
            raise ValueError("use either python or http/command, not both")
        return self

    @property
    def kind(self) -> Literal["python", "http"]:
        return "python" if self.python is not None else "http"


class AgentSection(_AgentTarget):
    name: str = Field(pattern=NAME_PATTERN)


class CandidateSection(_AgentTarget):
    """The version to compare against [agent]; its name defaults to the agent's."""

    name: str | None = Field(default=None, pattern=NAME_PATTERN)


class LimitsSection(_Section):
    run_timeout_seconds: float = Field(default=30, gt=0, le=3600)
    concurrency: int = Field(default=4, ge=1, le=256)


class ProxySection(_Section):
    mode: Literal["mock"] = "mock"
    fault_profile: str = Field(default="", max_length=40)
    fault_seed: int = Field(default=0, ge=0)
    mock_fixtures: str | None = None


class DatasetSection(_Section):
    name: str = Field(pattern=NAME_PATTERN)
    path: str = Field(min_length=1, max_length=500)


class EvalSection(_Section):
    min_pass_rate: float = Field(default=1.0, ge=0, le=1)
    baseline: str | None = None
    reports: str = ".runweave/reports"


class BenchSection(_Section):
    concurrency: tuple[int, ...] = Field(default=(1, 4, 16), min_length=1, max_length=12)
    runs: int = Field(default=32, ge=1, le=10000)
    fault_profiles: tuple[str, ...] | None = Field(default=None, min_length=1, max_length=12)
    soak_seconds: float = Field(default=0, ge=0, le=3600)
    max_failure_rate: float | None = Field(default=None, ge=0, le=1)
    max_p95_latency_ms: float | None = Field(default=None, gt=0)
    max_duplicate_writes: int | None = Field(default=None, ge=0)

    @field_validator("concurrency")
    @classmethod
    def sorted_levels(cls, levels: tuple[int, ...]) -> tuple[int, ...]:
        if any(not 1 <= level <= 256 for level in levels):
            raise ValueError("concurrency levels must be between 1 and 256")
        return tuple(sorted(set(levels)))


class ShadowSection(_Section):
    candidate: CandidateSection | None = None
    max_cost_ratio: float = Field(default=1.05, ge=1, le=100)
    max_latency_ratio: float = Field(default=2.0, ge=1, le=100)
    latency_slack_ms: float = Field(default=50, ge=0, le=60000)
    require_output_parity: bool = False


class _File(_Section):
    project: dict[str, Any] = Field(default_factory=dict)
    agent: AgentSection
    limits: LimitsSection = Field(default_factory=LimitsSection)
    proxy: ProxySection = Field(default_factory=ProxySection)
    datasets: list[DatasetSection] = Field(default_factory=list, max_length=100)
    eval: EvalSection = Field(default_factory=EvalSection)
    bench: BenchSection = Field(default_factory=BenchSection)
    shadow: ShadowSection = Field(default_factory=ShadowSection)


@dataclass(frozen=True)
class Dataset:
    name: str
    path: Path


@dataclass(frozen=True)
class EvalConfig:
    min_pass_rate: float
    baseline: Path | None
    reports: Path


@dataclass(frozen=True)
class Project:
    root: Path
    file: Path
    agent: AgentSection
    limits: LimitsSection
    proxy: ProxySection
    datasets: tuple[Dataset, ...]
    eval: EvalConfig
    mock_fixtures: Path | None
    bench: BenchSection
    shadow: ShadowSection

    def dataset(self, name: str | None) -> Dataset:
        if name is None:
            return self.datasets[0]
        for dataset in self.datasets:
            if dataset.name == name:
                return dataset
        known = ", ".join(d.name for d in self.datasets)
        raise ProjectError(f"No dataset named {name!r} in {self.file.name}; known: {known}")


def find_project(start: Path) -> Path:
    for folder in (start, *start.parents):
        candidate = folder / FILENAME
        if candidate.is_file():
            return candidate
    raise ProjectError(
        f"No {FILENAME} here or in any parent folder. Create a project with `runweave init`."
    )


def _inside(root: Path, value: str, field: str) -> Path:
    path = (root / value).resolve()
    if path != root and root not in path.parents:
        raise ProjectError(f"{field} must stay inside the project folder: {value}")
    return path


def _describe(error: ValidationError) -> str:
    parts = []
    for item in error.errors(include_url=False, include_input=False, include_context=False)[:5]:
        where = ".".join(str(p) for p in item["loc"])
        message = re.sub(r"^Value error, ", "", item["msg"])
        parts.append(f"{where}: {message}" if where else message)
    return "; ".join(parts)


def load_project(path: Path | None, *, start: Path | None = None) -> Project:
    file = (path or find_project((start or Path.cwd()).resolve())).resolve()
    try:
        data = tomllib.loads(file.read_text(encoding="utf-8"))
    except OSError as error:
        raise ProjectError(f"Cannot read {file}: {error.strerror}") from None
    except tomllib.TOMLDecodeError as error:
        raise ProjectError(f"{file.name} is not valid TOML: {error}") from None
    try:
        parsed = _File.model_validate(data)
    except ValidationError as error:
        raise ProjectError(f"{file.name}: {_describe(error)}") from None
    if not parsed.datasets:
        raise ProjectError(f"{file.name}: add at least one dataset ([[datasets]] name and path)")
    root = file.parent
    datasets = tuple(
        Dataset(d.name, _inside(root, d.path, f"datasets.{d.name}.path")) for d in parsed.datasets
    )
    return Project(
        root=root,
        file=file,
        agent=parsed.agent,
        limits=parsed.limits,
        proxy=parsed.proxy,
        datasets=datasets,
        eval=EvalConfig(
            min_pass_rate=parsed.eval.min_pass_rate,
            baseline=_inside(root, parsed.eval.baseline, "eval.baseline")
            if parsed.eval.baseline
            else None,
            reports=_inside(root, parsed.eval.reports, "eval.reports"),
        ),
        mock_fixtures=_inside(root, parsed.proxy.mock_fixtures, "proxy.mock_fixtures")
        if parsed.proxy.mock_fixtures
        else None,
        bench=parsed.bench
        if parsed.bench.fault_profiles is not None
        else parsed.bench.model_copy(update={"fault_profiles": (parsed.proxy.fault_profile,)}),
        shadow=parsed.shadow,
    )
