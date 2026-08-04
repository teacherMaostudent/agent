from __future__ import annotations

from pathlib import Path

from pydantic import Field, model_validator
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
    database_backend: str = "sqlite"
    database_url: str = Field(default="", repr=False)
    database_schema: str = "control_plane"
    postgres_schema_path: Path = _PROJECT_ROOT / "db" / "postgres.sql"
    contracts_schema_dir: Path = _PROJECT_ROOT.parent / "platform-contracts" / "schemas"
    tool_catalog_path: Path = _PROJECT_ROOT.parent / "tool-gateway" / "config" / "tools.json"
    tool_catalog_required: bool = False
    oidc_enabled: bool = False
    oidc_issuer: str = ""
    oidc_audience: str = "agent-platform"
    oidc_jwks_url: str = ""
    workload_token_url: str = ""
    workload_client_id: str = "agent-control-plane"
    workload_client_secret: str = Field(default="", repr=False)
    workload_audience: str = "agent-platform"
    workload_scope: str = ""
    mtls_enabled: bool = False
    mtls_ca_file: str = ""
    mtls_cert_file: str = ""
    mtls_key_file: str = Field(default="", repr=False)
    otel_enabled: bool = False
    otel_endpoint: str = "http://localhost:4318/v1/traces"
    temporal_enabled: bool = False
    temporal_target: str = "localhost:7233"
    temporal_namespace: str = "default"
    temporal_task_queue: str = "control-plane-releases"
    enforce_admin_role: bool = False
    admin_api_key: str | None = Field(default=None, repr=False)
    runtime_api_key: str | None = Field(default=None, repr=False)
    cors_origins: list[str] = Field(default_factory=list)
    llm_gateway_base_url: str = "http://localhost:8080"
    llm_gateway_admin_username: str = "admin"
    llm_gateway_admin_password: str = Field(default="admin123", repr=False)
    governance_base_url: str = "http://localhost:8081"
    governance_user_id: str = "control-plane"
    governance_auditor_api_key: str | None = Field(default=None, repr=False)
    model_release_min_canary_requests: int = Field(default=20, ge=1)
    model_release_max_error_rate: float = Field(default=0.05, ge=0, le=1)
    model_release_max_timeout_rate: float = Field(default=0.02, ge=0, le=1)
    model_release_max_average_latency_ms: int = Field(default=10_000, ge=1)
    model_release_auto_promote: bool = True
    agent_release_quality_gate_required: bool = False
    model_release_monitor_interval_seconds: float = Field(default=30, gt=0, le=3_600)

    @model_validator(mode="after")
    def validate_production_security(self) -> Settings:
        if self.environment.lower() in {"production", "prod"}:
            unsafe: list[str] = []
            if not self.enforce_admin_role:
                unsafe.append("CONTROL_PLANE_ENFORCE_ADMIN_ROLE must be true")
            if not self.runtime_api_key:
                unsafe.append("CONTROL_PLANE_RUNTIME_API_KEY is required")
            if not self.admin_api_key:
                unsafe.append("CONTROL_PLANE_ADMIN_API_KEY is required")
            if not self.governance_auditor_api_key:
                unsafe.append("CONTROL_PLANE_GOVERNANCE_AUDITOR_API_KEY is required")
            if self.llm_gateway_admin_password == "admin123":
                unsafe.append("CONTROL_PLANE_LLM_GATEWAY_ADMIN_PASSWORD must be rotated")
            if not self.agent_release_quality_gate_required:
                unsafe.append("CONTROL_PLANE_AGENT_RELEASE_QUALITY_GATE_REQUIRED must be true")
            if self.database_backend != "postgres" or not self.database_url:
                unsafe.append("CONTROL_PLANE_DATABASE_BACKEND must be postgres")
            if not self.oidc_enabled or not self.oidc_issuer or not self.oidc_jwks_url:
                unsafe.append(
                    "CONTROL_PLANE_OIDC_ENABLED, OIDC_ISSUER and OIDC_JWKS_URL are required"
                )
            if not self.workload_token_url or not self.workload_client_secret:
                unsafe.append(
                    "CONTROL_PLANE_WORKLOAD_TOKEN_URL and WORKLOAD_CLIENT_SECRET are required"
                )
            if not self.temporal_enabled:
                unsafe.append("CONTROL_PLANE_TEMPORAL_ENABLED must be true")
            if not self.tool_catalog_required:
                unsafe.append("CONTROL_PLANE_TOOL_CATALOG_REQUIRED must be true")
            if self.mtls_enabled and not all(
                (self.mtls_ca_file, self.mtls_cert_file, self.mtls_key_file)
            ):
                unsafe.append("CONTROL_PLANE mTLS certificate paths are required")
            if unsafe:
                raise ValueError("Unsafe production configuration: " + "; ".join(unsafe))
        return self
