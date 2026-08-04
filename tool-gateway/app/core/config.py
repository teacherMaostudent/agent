from functools import lru_cache
from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "tool-gateway"
    environment: str = "local"
    api_prefix: str = "/api/v1"
    host: str = "0.0.0.0"
    port: int = Field(default=8090, ge=1, le=65_535)
    database_path: Path = Path("data/tool-gateway.db")
    database_backend: str = "sqlite"
    database_url: str = Field(default="", repr=False)
    database_schema: str = "tool_gateway"
    tools_config_path: Path = Path("config/tools.json")
    contracts_schema_dir: Path = Path("../platform-contracts/schemas")
    require_service_auth: bool = True
    service_api_key: str = "local-tool-gateway-key"
    admin_api_key: str = "local-tool-gateway-admin-key"
    allow_private_networks: bool = False
    approval_ttl_seconds: int = Field(default=900, ge=60, le=86_400)
    idempotency_ttl_seconds: int = Field(default=86_400, ge=60, le=604_800)
    max_request_bytes: int = Field(default=1_000_000, ge=1_024, le=20_000_000)
    max_response_bytes: int = Field(default=1_000_000, ge=1_024, le=20_000_000)
    http_connect_timeout: float = Field(default=5, gt=0, le=60)
    governance_base_url: str = ""
    governance_event_key: str = ""
    redis_url: str = Field(default="", repr=False)
    oidc_enabled: bool = False
    oidc_issuer: str = ""
    oidc_audience: str = "agent-platform"
    oidc_jwks_url: str = ""
    workload_token_url: str = ""
    workload_client_id: str = "tool-gateway"
    workload_client_secret: str = Field(default="", repr=False)
    workload_audience: str = "agent-platform"
    workload_scope: str = ""
    mtls_enabled: bool = False
    mtls_ca_file: str = ""
    mtls_cert_file: str = ""
    mtls_key_file: str = Field(default="", repr=False)
    opa_enabled: bool = False
    opa_base_url: str = "http://localhost:8181"
    opa_decision_path: str = "agent_platform/tool/allow"
    otel_enabled: bool = False
    otel_endpoint: str = "http://localhost:4318/v1/traces"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="TOOL_GATEWAY_",
        extra="ignore",
    )

    @model_validator(mode="after")
    def validate_auth(self) -> "Settings":
        if self.require_service_auth and not self.service_api_key:
            raise ValueError("TOOL_GATEWAY_SERVICE_API_KEY is required")
        if not self.admin_api_key:
            raise ValueError("TOOL_GATEWAY_ADMIN_API_KEY is required")
        if self.environment.lower() in {"production", "prod"}:
            unsafe: list[str] = []
            if not self.require_service_auth:
                unsafe.append("TOOL_GATEWAY_REQUIRE_SERVICE_AUTH must be true")
            if self.service_api_key == "local-tool-gateway-key":
                unsafe.append("TOOL_GATEWAY_SERVICE_API_KEY must be rotated")
            if self.admin_api_key == "local-tool-gateway-admin-key":
                unsafe.append("TOOL_GATEWAY_ADMIN_API_KEY must be rotated")
            if self.database_backend != "postgres" or not self.database_url:
                unsafe.append("TOOL_GATEWAY_DATABASE_BACKEND must be postgres")
            if not self.redis_url:
                unsafe.append("TOOL_GATEWAY_REDIS_URL is required")
            if not self.oidc_enabled or not self.oidc_issuer or not self.oidc_jwks_url:
                unsafe.append(
                    "TOOL_GATEWAY_OIDC_ENABLED, OIDC_ISSUER and OIDC_JWKS_URL are required"
                )
            if not self.workload_token_url or not self.workload_client_secret:
                unsafe.append(
                    "TOOL_GATEWAY_WORKLOAD_TOKEN_URL and WORKLOAD_CLIENT_SECRET are required"
                )
            if not self.opa_enabled:
                unsafe.append("TOOL_GATEWAY_OPA_ENABLED must be true")
            if self.mtls_enabled and not all(
                (self.mtls_ca_file, self.mtls_cert_file, self.mtls_key_file)
            ):
                unsafe.append("TOOL_GATEWAY mTLS certificate paths are required")
            if unsafe:
                raise ValueError("Unsafe production configuration: " + "; ".join(unsafe))
        return self

    def ensure_directories(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_directories()
    return settings
