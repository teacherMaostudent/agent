from __future__ import annotations

from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="GOVERNANCE_", env_file=".env", extra="ignore")

    environment: str = "local"
    database_path: Path = _PROJECT_ROOT / "data" / "governance.db"
    schema_path: Path = _PROJECT_ROOT / "db" / "schema.sql"
    database_backend: str = "sqlite"
    database_url: str = Field(default="", repr=False)
    database_schema: str = "governance"
    postgres_schema_path: Path = _PROJECT_ROOT / "db" / "postgres.sql"
    contracts_schema_dir: Path = _PROJECT_ROOT.parent / "platform-contracts" / "schemas"
    oidc_enabled: bool = False
    oidc_issuer: str = ""
    oidc_audience: str = "agent-platform"
    oidc_jwks_url: str = ""
    workload_token_url: str = ""
    workload_client_id: str = "agent-governance"
    workload_client_secret: str = Field(default="", repr=False)
    workload_audience: str = "agent-platform"
    workload_scope: str = ""
    kafka_bootstrap_servers: str = ""
    kafka_governance_topic: str = "agent.governance.events.v1"
    kafka_retry_topic: str = "agent.governance.events.retry.v1"
    kafka_dlq_topic: str = "agent.governance.events.dlq.v1"
    kafka_max_attempts: int = Field(default=5, ge=1, le=20)
    # Keep polling and retry controls in configuration so Kafka consumers can
    # be tuned per deployment without duplicating consumer implementations.
    kafka_max_poll_records: int = Field(default=100, ge=1, le=10_000)
    kafka_retry_backoff_max_seconds: int = Field(default=30, ge=1, le=3_600)
    kafka_consumer_group: str = "agent-governance"
    worm_bucket: str = ""
    worm_prefix: str = "governance-audit"
    worm_endpoint_url: str = ""
    worm_region: str = ""
    worm_kms_key_id: str = Field(default="", repr=False)
    worm_retention_days: int = Field(default=2555, ge=1)
    otel_enabled: bool = False
    otel_endpoint: str = "http://localhost:4318/v1/traces"
    enforce_auditor_role: bool = False
    auditor_api_key: str | None = Field(default=None, repr=False)
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
    online_trace_sample_rate: float = Field(default=1.0, ge=0, le=1)
    capture_prompt_response_content: bool = False
    online_trace_retention_days: int = Field(default=30, ge=1, le=3650)

    @model_validator(mode="after")
    def validate_production_security(self) -> Settings:
        if self.environment.lower() in {"production", "prod"}:
            unsafe: list[str] = []
            if not self.enforce_auditor_role:
                unsafe.append("GOVERNANCE_ENFORCE_AUDITOR_ROLE must be true")
            if not self.event_ingestion_key:
                unsafe.append("GOVERNANCE_EVENT_INGESTION_KEY is required")
            if not self.auditor_api_key:
                unsafe.append("GOVERNANCE_AUDITOR_API_KEY is required")
            if self.llm_gateway_api_key == "governance-service-key":
                unsafe.append("GOVERNANCE_LLM_GATEWAY_API_KEY must be rotated")
            if self.online_trace_sample_rate >= 1:
                unsafe.append("GOVERNANCE_ONLINE_TRACE_SAMPLE_RATE must be below 1 in production")
            if self.database_backend != "postgres" or not self.database_url:
                unsafe.append("GOVERNANCE_DATABASE_BACKEND must be postgres")
            if not self.oidc_enabled or not self.oidc_issuer or not self.oidc_jwks_url:
                unsafe.append(
                    "GOVERNANCE_OIDC_ENABLED, OIDC_ISSUER and OIDC_JWKS_URL are required"
                )
            if not self.workload_token_url or not self.workload_client_secret:
                unsafe.append(
                    "GOVERNANCE_WORKLOAD_TOKEN_URL and WORKLOAD_CLIENT_SECRET are required"
                )
            if not self.kafka_bootstrap_servers:
                unsafe.append("GOVERNANCE_KAFKA_BOOTSTRAP_SERVERS is required")
            if not self.worm_bucket or not self.worm_kms_key_id:
                unsafe.append("GOVERNANCE_WORM_BUCKET and WORM_KMS_KEY_ID are required")
            if unsafe:
                raise ValueError("Unsafe production configuration: " + "; ".join(unsafe))
        return self
