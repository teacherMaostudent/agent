"""Knowledge Wiki deployment configuration."""

from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Keep Wiki persistence and downstream credentials outside Runtime/RAG configuration."""

    environment: str = "local"
    process_role: str = Field(default="api", pattern="^(api|relay)$")
    database_url: str = "sqlite:///data/knowledge-wiki.db"
    oidc_enabled: bool = False
    oidc_issuer: str = ""
    oidc_audience: str = "agent-platform"
    oidc_jwks_url: str = ""
    workload_token_url: str = ""
    workload_client_id: str = ""
    workload_client_secret: str = Field(default="", repr=False)
    workload_audience: str = ""
    workload_scope: str = ""
    mtls_enabled: bool = False
    mtls_ca_file: str = ""
    mtls_cert_file: str = ""
    mtls_key_file: str = ""
    service_api_key: str = Field(default="", repr=False)
    contracts_schema_dir: Path = (
        Path(__file__).resolve().parents[2] / "platform-contracts" / "schemas"
    )
    ingestion_base_url: str = "http://ingestion-api:8004/api/v1"
    ingestion_service_key: str = Field(default="", repr=False)
    governance_base_url: str = "http://agent-governance:8081"
    governance_event_key: str = Field(default="", repr=False)
    governance_auditor_key: str = Field(default="", repr=False)
    relay_batch_size: int = Field(default=20, ge=1, le=100)
    relay_poll_seconds: float = Field(default=2.0, ge=0.2, le=60)
    relay_lease_seconds: int = Field(default=60, ge=10, le=600)
    relay_max_attempts: int = Field(default=8, ge=1, le=50)
    relay_max_backoff_seconds: int = Field(default=300, ge=1, le=3_600)

    model_config = SettingsConfigDict(env_prefix="KNOWLEDGE_WIKI_", extra="ignore")

    @model_validator(mode="after")
    def validate_production(self):
        """Production must not silently fall back to local files or unsigned identities."""
        if self.environment.lower() in {"production", "prod"}:
            if not self.database_url.startswith(("postgresql://", "postgresql+psycopg://")):
                raise ValueError("production Knowledge Wiki requires PostgreSQL")
            if not self.oidc_enabled or not self.oidc_issuer or not self.oidc_jwks_url:
                raise ValueError("production Knowledge Wiki requires OIDC")
            if not self.service_api_key:
                raise ValueError("production Knowledge Wiki requires a service API key")
            if self.process_role == "relay":
                if not (
                    self.workload_token_url
                    and self.workload_client_id
                    and self.workload_client_secret
                    and self.workload_audience
                ):
                    raise ValueError("production Knowledge Wiki relay requires workload identity")
                if not (
                    self.mtls_enabled
                    and self.mtls_ca_file
                    and self.mtls_cert_file
                    and self.mtls_key_file
                ):
                    raise ValueError("production Knowledge Wiki relay requires mTLS")
        return self
