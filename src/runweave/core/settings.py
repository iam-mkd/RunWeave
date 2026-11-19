from decimal import Decimal
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, SecretStr, StringConstraints, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from runweave.agents.provider_wire import Provider
from runweave.budgets import BudgetPolicy
from runweave.proxy.faults import FaultSpec
from runweave.security import IdentityName, Principal

AgentTarget = Annotated[
    str,
    StringConstraints(
        pattern=r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*:[A-Za-z_][A-Za-z0-9_]*$",
        max_length=200,
    ),
]


HostName = Annotated[
    str,
    StringConstraints(
        pattern=r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
        r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$",
        max_length=253,
    ),
]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="RUNWEAVE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="forbid",
        hide_input_in_errors=True,
    )

    app_name: str = Field(default="RunWeave", min_length=1, max_length=100)
    auth_mode: Literal["local", "test", "jwt"] = "local"
    auth_test_identities: dict[str, Principal] = Field(default_factory=dict, repr=False)
    auth_revoked_subjects: frozenset[tuple[str, str]] = Field(
        default_factory=frozenset, max_length=1000
    )
    auth_issuer: str = Field(default="", max_length=500)
    auth_audience: str = Field(default="", max_length=200)
    auth_signing_key: SecretStr = Field(default_factory=lambda: SecretStr(""))
    tenant_max_unfinished: int = Field(default=1000, ge=1, le=10000)
    protected_values: tuple[SecretStr, ...] = Field(default=(), max_length=32)
    http_timeout_seconds: float = Field(default=5.0, gt=0, le=60)
    http_max_connections: int = Field(default=128, ge=1, le=1024)
    worker_count: int = Field(default=128, ge=1, le=1024)
    queue_capacity: int = Field(default=1000, ge=1, le=10000)
    history_max_records: int = Field(default=1000, ge=1, le=10000)
    history_ttl_seconds: float = Field(default=3600, gt=0, le=86400)
    history_cleanup_interval_seconds: float = Field(default=30, gt=0, le=3600)
    run_timeout_seconds: float = Field(default=30, gt=0, le=3600)
    step_timeout_seconds: float = Field(default=5, gt=0, le=3600)
    shutdown_grace_seconds: float = Field(default=5, ge=0, le=60)
    tool_retry_attempts: int = Field(default=3, ge=1, le=10)
    tool_retry_base_delay_seconds: float = Field(default=0.1, gt=0, le=60)
    tool_retry_max_delay_seconds: float = Field(default=2, gt=0, le=60)
    agent_max_steps: int = Field(default=7, ge=1, le=20)
    agent_completion_tokens: int = Field(default=2048, ge=1, le=8192)
    agent_request_limit: int = Field(default=20, ge=1, le=1000)
    agent_input_tokens: int = Field(default=100000, ge=1, le=10000000)
    agent_context_tokens: int = Field(default=16384, ge=1, le=131072)
    agent_evidence_bytes: int = Field(default=2048, ge=128, le=16384)
    agent_max_cost_usd: Decimal = Field(
        default=Decimal("0.05"), ge=0, le=100, decimal_places=9, max_digits=12
    )
    model_input_usd_per_million: Decimal | None = Field(
        default=None, ge=0, le=1000, decimal_places=3
    )
    model_output_usd_per_million: Decimal | None = Field(
        default=None, ge=0, le=1000, decimal_places=3
    )
    model_price_version: str = Field(default="", max_length=100)
    agent_workflow: Literal["baseline", "pydanticai", "coordinated"] = "baseline"
    # Python agents to import at startup as module:attribute. Never loaded from requests.
    agents: tuple[AgentTarget, ...] = Field(default=(), max_length=32)
    # HTTP agents (runweave.agent/v1) discovered at startup; more can be added at runtime.
    http_agents: tuple[Annotated[str, StringConstraints(max_length=2048)], ...] = Field(
        default=(), max_length=16
    )
    # Non-loopback hosts HTTP agents may use. Loopback is always allowed.
    http_agent_allowed_hosts: tuple[HostName, ...] = Field(default=(), max_length=16)
    http_agent_max_registered: int = Field(default=32, ge=1, le=128)
    http_agent_retry_attempts: int = Field(default=3, ge=1, le=5)
    http_agent_retry_base_delay_seconds: float = Field(default=0.2, gt=0, le=5)
    http_agent_retry_max_delay_seconds: float = Field(default=2.0, gt=0, le=30)
    http_agent_response_max_bytes: int = Field(default=1048576, ge=4096, le=8388608)
    http_agent_connect_timeout_seconds: float = Field(default=2.0, gt=0, le=30)
    # LLM proxy: the URL agents use to reach this server (serve sets it from --port).
    proxy_public_url: str = Field(
        default="http://127.0.0.1:8000", pattern=r"^https?://[^\s/?#]+$", max_length=200
    )
    proxy_mock_fixtures: Path | None = None
    proxy_request_max_bytes: int = Field(default=1048576, ge=4096, le=16777216)
    proxy_default_max_tokens: int = Field(default=1024, ge=1, le=100000)
    # "passthrough" forwards to your providers with your keys; prices and models required.
    proxy_mode: Literal["mock", "passthrough"] = "mock"
    proxy_models: tuple[Annotated[str, StringConstraints(min_length=1, max_length=200)], ...] = (
        Field(default=(), max_length=64)
    )
    proxy_openai_base_url: str = Field(
        default="https://api.openai.com/v1", pattern=r"^https://[^\s?#]+$", max_length=200
    )
    proxy_openai_api_key: SecretStr = Field(default_factory=lambda: SecretStr(""))
    proxy_openai_concurrency: int = Field(default=4, ge=1, le=64)
    proxy_openai_requests_per_minute: int = Field(default=60, ge=1, le=6000)
    proxy_anthropic_base_url: str = Field(
        default="https://api.anthropic.com", pattern=r"^https://[^\s?#]+$", max_length=200
    )
    proxy_anthropic_api_key: SecretStr = Field(default_factory=lambda: SecretStr(""))
    proxy_anthropic_concurrency: int = Field(default=4, ge=1, le=64)
    proxy_anthropic_requests_per_minute: int = Field(default=60, ge=1, le=6000)
    # Where keys saved from the Settings screen live: the OS credential store by default.
    key_store: Literal["keyring", "memory", "none"] = "keyring"
    proxy_upstream_max_bytes: int = Field(default=8388608, ge=65536, le=67108864)
    # Seeded fault injection: a built-in or file profile name, or "custom" for the inline list.
    project_file: Path | None = None  # runweave.toml for the Load tests, Evals and Shadow screens
    proxy_fault_profile: str = Field(default="", max_length=40)
    proxy_fault_seed: int = Field(default=0, ge=0, le=2**63 - 1)
    proxy_fault_profiles_file: Path | None = None
    proxy_fault_inline: tuple[FaultSpec, ...] = Field(default=(), max_length=16)

    @model_validator(mode="after")
    def require_passthrough_configuration(self) -> "Settings":
        if self.proxy_mode != "passthrough":
            return self
        # Keys may also come from the credential store; a provider without one is refused
        # per request (PROVIDER_NOT_CONFIGURED).
        if not self.proxy_models:
            raise ValueError("Passthrough needs RUNWEAVE_PROXY_MODELS, the models you allow")
        if (
            self.model_input_usd_per_million is None
            or self.model_output_usd_per_million is None
            or not self.model_price_version.strip()
        ):
            raise ValueError("Configure versioned input and output prices before passthrough")
        return self

    run_request_max_bytes: int = Field(default=65536, ge=1024, le=1048576)
    agent_output_max_bytes: int = Field(default=65536, ge=1024, le=1048576)
    event_stream_max_subscribers: int = Field(default=64, ge=1, le=1024)
    event_stream_max_seconds: float = Field(default=60, ge=1, le=600)
    event_stream_heartbeat_seconds: float = Field(default=15, ge=0.1, le=60)
    coordination_concurrency: Literal[1, 2] = 2
    isolated_tools_enabled: bool = False
    isolated_tools_concurrency: int = Field(default=2, ge=1, le=8)
    sandbox_python_image: str = Field(default="", pattern=r"^(sha256:[a-f0-9]{64})?$")
    sandbox_browser_image: str = Field(default="", pattern=r"^(sha256:[a-f0-9]{64})?$")
    browser_allowed_origins: tuple[Literal["https://fixtures.runweave.invalid"], ...] = ()
    sandbox_fixture_credential: SecretStr = Field(default_factory=lambda: SecretStr(""))

    @model_validator(mode="after")
    def require_isolation_policy(self) -> "Settings":
        if self.isolated_tools_enabled and (
            not self.sandbox_python_image
            or not self.sandbox_browser_image
            or not self.browser_allowed_origins
        ):
            raise ValueError(
                "Configure immutable worker images and approved fixture origins "
                "before enabling isolated tools"
            )
        return self

    model_provider: Provider = "fake"
    model_api_key: SecretStr = Field(default_factory=lambda: SecretStr(""))
    model_name: str = Field(default="", max_length=100)
    model_concurrency: int = Field(default=2, ge=1, le=32)
    model_requests_per_minute: int = Field(default=30, ge=1, le=600)
    groq_api_key: SecretStr = Field(default_factory=lambda: SecretStr(""))
    groq_model: str = Field(default="llama-3.3-70b-versatile", min_length=1, max_length=100)
    groq_concurrency: int = Field(default=2, ge=1, le=32)
    groq_requests_per_minute: int = Field(default=30, ge=1, le=600)
    json_logs: bool = False
    console_traces: bool = False
    mock_runbook_delay_seconds: float = Field(default=0, ge=0, le=5)
    database_url: SecretStr | None = None
    db_pool_size: int = Field(default=8, ge=1, le=64)
    lease_seconds: float = Field(default=6, ge=1, le=60)
    db_poll_seconds: float = Field(default=0.25, ge=0.05, le=5)
    durable_max_attempts: int = Field(default=3, ge=1, le=10)
    durable_retry_delay_seconds: float = Field(default=0.5, ge=0.05, le=30)
    approval_ttl_seconds: float = Field(default=3600, ge=1, le=86400)
    approval_max_pending: int = Field(default=100, ge=1, le=10000)
    memory_ttl_seconds: float = Field(default=604800, ge=1, le=2592000)
    memory_max_records: int = Field(default=10000, ge=1, le=100000)
    memory_max_per_tenant: int = Field(default=1000, ge=1, le=10000)
    memory_max_per_scope: int = Field(default=100, ge=1, le=1000)
    memory_recall_limit: int = Field(default=5, ge=1, le=20)
    memory_recall_bytes: int = Field(default=4096, ge=128, le=16384)
    memory_project_memberships: dict[IdentityName, dict[IdentityName, frozenset[IdentityName]]] = (
        Field(default_factory=dict)
    )

    @model_validator(mode="after")
    def bound_memory_memberships(self) -> "Settings":
        projects = sum(len(projects) for projects in self.memory_project_memberships.values())
        members = sum(
            len(subjects)
            for projects in self.memory_project_memberships.values()
            for subjects in projects.values()
        )
        if len(self.memory_project_memberships) > 1000 or projects > 1000 or members > 1000:
            raise ValueError("Configure at most 1000 memory tenants, projects, and memberships")
        return self

    @model_validator(mode="after")
    def require_groq_key(self) -> "Settings":
        key = self.groq_api_key.get_secret_value().strip()
        if self.model_provider == "groq" and (not key or key == "replace_with_your_groq_api_key"):
            raise ValueError("Set RUNWEAVE_GROQ_API_KEY before enabling Groq")
        return self

    @model_validator(mode="after")
    def require_provider_configuration(self) -> "Settings":
        if self.model_provider not in ("fake", "groq") and (
            not self.model_api_key.get_secret_value().strip() or not self.model_name.strip()
        ):
            raise ValueError("Set RUNWEAVE_MODEL_API_KEY and RUNWEAVE_MODEL_NAME")
        return self

    @property
    def selected_model(self) -> str:
        if self.model_provider == "fake":
            return "fake-v1"
        return self.groq_model if self.model_provider == "groq" else self.model_name.strip()

    @model_validator(mode="after")
    def require_live_prices(self) -> "Settings":
        if self.model_provider != "fake" and (
            self.model_input_usd_per_million is None
            or self.model_output_usd_per_million is None
            or not self.model_price_version.strip()
        ):
            raise ValueError("Configure versioned input and output prices before enabling live AI")
        return self

    def budget_policy(self) -> BudgetPolicy:
        live = self.model_provider != "fake" or self.proxy_mode == "passthrough"
        return BudgetPolicy(
            model=self.selected_model,
            price_version=self.model_price_version if live else "fake-free-v1",
            input_nanos_per_token=int((self.model_input_usd_per_million or 0) * 1000)
            if live
            else 0,
            output_nanos_per_token=int((self.model_output_usd_per_million or 0) * 1000)
            if live
            else 0,
            max_requests=self.agent_request_limit,
            max_input_tokens=self.agent_input_tokens,
            max_output_tokens=self.agent_completion_tokens,
            max_cost_nanos=int(self.agent_max_cost_usd * 1000000000),
            context_tokens=self.agent_context_tokens,
            evidence_bytes=self.agent_evidence_bytes,
        )

    @model_validator(mode="after")
    def require_identity_configuration(self) -> "Settings":
        if self.auth_mode == "jwt" and (
            not self.auth_issuer.strip()
            or not self.auth_audience.strip()
            or len(self.auth_signing_key.get_secret_value().encode()) < 32
        ):
            raise ValueError("JWT mode requires issuer, audience, and a 32-byte signing key")
        if self.auth_mode == "test" and not self.auth_test_identities:
            raise ValueError("Test mode requires configured test identities")
        if len(self.auth_test_identities) > 32 or any(
            not 16 <= len(token) <= 512 for token in self.auth_test_identities
        ):
            raise ValueError("Configure at most 32 test tokens of 16 to 512 characters")
        from runweave.agents.http import normalize_agent_url

        self.http_agents = tuple(
            dict.fromkeys(
                normalize_agent_url(url, allowed_hosts=self.http_agent_allowed_hosts)
                for url in self.http_agents
            )
        )
        return self
