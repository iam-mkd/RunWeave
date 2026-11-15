from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from runweave.security import Principal


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
    agent_workflow: Literal["baseline", "pydanticai"] = "baseline"
    model_provider: Literal["fake", "groq"] = "fake"
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

    @model_validator(mode="after")
    def require_groq_key(self) -> "Settings":
        key = self.groq_api_key.get_secret_value().strip()
        if self.model_provider == "groq" and (not key or key == "replace_with_your_groq_api_key"):
            raise ValueError("Set RUNWEAVE_GROQ_API_KEY before enabling Groq")
        return self

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
        return self
