from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="RUNWEAVE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="forbid",
        hide_input_in_errors=True,
    )

    app_name: str = Field(default="RunWeave", min_length=1, max_length=100)
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
    model_provider: Literal["fake", "groq"] = "fake"
    groq_api_key: SecretStr = Field(default_factory=lambda: SecretStr(""))
    groq_model: str = Field(default="llama-3.3-70b-versatile", min_length=1, max_length=100)
    groq_concurrency: int = Field(default=2, ge=1, le=32)
    groq_requests_per_minute: int = Field(default=30, ge=1, le=600)
    json_logs: bool = False
    console_traces: bool = False
    mock_runbook_delay_seconds: float = Field(default=0, ge=0, le=5)

    @model_validator(mode="after")
    def require_groq_key(self) -> "Settings":
        key = self.groq_api_key.get_secret_value().strip()
        if self.model_provider == "groq" and (not key or key == "replace_with_your_groq_api_key"):
            raise ValueError("Set RUNWEAVE_GROQ_API_KEY before enabling Groq")
        return self
