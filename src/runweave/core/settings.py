from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="RUNWEAVE_", env_file=".env", env_file_encoding="utf-8", extra="forbid"
    )

    app_name: str = Field(default="RunWeave", min_length=1, max_length=100)
    http_timeout_seconds: float = Field(default=5.0, gt=0, le=60)
    http_max_connections: int = Field(default=128, ge=1, le=1024)
    worker_count: int = Field(default=128, ge=1, le=1024)
    queue_capacity: int = Field(default=1000, ge=1, le=10000)
    history_max_records: int = Field(default=1000, ge=1, le=10000)
    history_ttl_seconds: float = Field(default=3600, gt=0, le=86400)
    history_cleanup_interval_seconds: float = Field(default=30, gt=0, le=3600)
