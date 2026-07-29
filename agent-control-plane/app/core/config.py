from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

_PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CONTROL_PLANE_",
        env_file=".env",
        extra="ignore",
    )

    service_name: str = "agent-control-plane"
    environment: str = "local"
    database_path: Path = _PROJECT_ROOT / "data" / "control-plane.db"
    schema_path: Path = _PROJECT_ROOT / "db" / "schema.sql"
    enforce_admin_role: bool = False
    runtime_api_key: str | None = Field(default=None, repr=False)
    cors_origins: list[str] = Field(default_factory=list)
    llm_gateway_base_url: str = "http://localhost:8080"
    llm_gateway_admin_username: str = "admin"
    llm_gateway_admin_password: str = Field(default="admin123", repr=False)
    governance_base_url: str = "http://localhost:8081"
    governance_user_id: str = "control-plane"
    model_release_min_canary_requests: int = Field(default=20, ge=1)
    model_release_max_error_rate: float = Field(default=0.05, ge=0, le=1)
    model_release_max_timeout_rate: float = Field(default=0.02, ge=0, le=1)
    model_release_max_average_latency_ms: int = Field(default=10_000, ge=1)
    model_release_auto_promote: bool = True
    model_release_monitor_interval_seconds: float = Field(default=30, gt=0, le=3_600)
