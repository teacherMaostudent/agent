from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

_PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="GOVERNANCE_", env_file=".env", extra="ignore")

    environment: str = "local"
    database_path: Path = _PROJECT_ROOT / "data" / "governance.db"
    schema_path: Path = _PROJECT_ROOT / "db" / "schema.sql"
    enforce_auditor_role: bool = False
    event_ingestion_key: str | None = Field(default=None, repr=False)
    llm_gateway_base_url: str = "http://localhost:8080"
    llm_gateway_api_key: str = Field(default="governance-service-key", repr=False)
    llm_gateway_timeout_seconds: float = Field(default=60.0, gt=0, le=600)
    judge_primary_model: str = "qwen-plus"
    judge_secondary_model: str = "claude-3-5-haiku"
    judge_arbitrator_model: str = "deepseek-v4-pro"
    judge_disagreement_threshold: int = Field(default=15, ge=0, le=100)
    quality_gate_min_average_score: float = Field(default=75, ge=0, le=100)
    quality_gate_min_pass_rate: float = Field(default=0.9, ge=0, le=1)
    quality_gate_max_arbitration_rate: float = Field(default=0.5, ge=0, le=1)
    quality_gate_max_failed_cases: int = Field(default=0, ge=0)
