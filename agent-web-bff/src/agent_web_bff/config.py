"""Configuration for the browser BFF; user identity is verified before proxying."""

from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class WebBffSettings(BaseSettings):
    """Declare same-origin session, Runtime projection and read-only Console endpoints."""

    environment: str = "local"
    runtime_base_url: str = "http://localhost:8001/api/v1"
    runtime_service_key: str = Field(default="", repr=False)
    governance_base_url: str = "http://localhost:8081"
    governance_auditor_key: str = Field(default="", repr=False)
    control_plane_base_url: str = "http://localhost:8080"
    control_plane_admin_key: str = Field(default="", repr=False)
    knowledge_wiki_base_url: str = ""
    knowledge_wiki_service_key: str = Field(default="", repr=False)
    oidc_enabled: bool = False
    oidc_issuer: str = ""
    oidc_audience: str = "agent-platform"
    oidc_jwks_url: str = ""
    oidc_authorization_url: str = ""
    oidc_token_url: str = ""
    oidc_end_session_url: str = ""
    oidc_client_id: str = ""
    oidc_client_secret: str = Field(default="", repr=False)
    oidc_redirect_uri: str = ""
    oidc_scopes: str = "openid profile email"
    identity_admin_base_url: str = ""
    identity_admin_realm: str = ""
    identity_admin_client_id: str = ""
    identity_admin_client_secret: str = Field(default="", repr=False)
    session_redis_url: str = Field(default="", repr=False)
    session_cookie_name: str = "agent_web_session"
    session_ttl_seconds: int = 28_800
    high_risk_acr_values: str = "urn:mace:incommon:iap:silver,2"
    high_risk_max_auth_age_seconds: int = Field(default=900, ge=60, le=86_400)
    public_origin: str = ""
    csrf_enforced: bool = False
    cors_origins: list[str] = []
    request_timeout_seconds: float = 20.0
    static_dir: Path = Path("static")
    mtls_enabled: bool = False
    mtls_ca_file: str = ""
    mtls_cert_file: str = ""
    mtls_key_file: str = Field(default="", repr=False)
    console_runtime_health_url: str = "http://localhost:8001/api/v1/health/ready"
    console_control_plane_health_url: str = "http://localhost:9002/health/ready"
    console_governance_health_url: str = "http://localhost:9001/health/ready"
    console_llm_gateway_health_url: str = "http://localhost:9000/actuator/health"
    console_context_health_url: str = "http://localhost:8002/api/v1/health"
    console_rag_health_url: str = "http://localhost:8003/api/v1/health"
    console_ingestion_health_url: str = "http://localhost:8004/api/v1/health"
    console_tool_gateway_health_url: str = "http://localhost:9090/api/v1/health/ready"

    model_config = SettingsConfigDict(env_prefix="WEB_BFF_", extra="ignore")

    @model_validator(mode="after")
    def validate_production_identity(self) -> "WebBffSettings":
        """生产 BFF 不接受浏览器伪造 Header，必须启用 OIDC 验签。"""
        if self.environment.lower() in {"production", "prod"} and (
            not self.oidc_enabled or not self.oidc_issuer
        ):
            raise ValueError("WEB_BFF OIDC issuer is required in production")
        if self.environment.lower() in {"production", "prod"} and not self.public_origin:
            raise ValueError("WEB_BFF public_origin is required in production")
        if self.environment.lower() in {"production", "prod"}:
            required = {
                "WEB_BFF_OIDC_AUTHORIZATION_URL": self.oidc_authorization_url,
                "WEB_BFF_OIDC_TOKEN_URL": self.oidc_token_url,
                "WEB_BFF_OIDC_CLIENT_ID": self.oidc_client_id,
                "WEB_BFF_OIDC_REDIRECT_URI": self.oidc_redirect_uri,
                "WEB_BFF_OIDC_END_SESSION_URL": self.oidc_end_session_url,
                "WEB_BFF_SESSION_REDIS_URL": self.session_redis_url,
            }
            missing = [name for name, value in required.items() if not value]
            if missing:
                raise ValueError(f"production browser session configuration is missing: {', '.join(missing)}")
            if not self.mtls_enabled or not all(
                (self.mtls_ca_file, self.mtls_cert_file, self.mtls_key_file)
            ):
                raise ValueError("production Web BFF requires a dedicated mTLS identity")
            if not self.oidc_redirect_uri.startswith(self.public_origin.rstrip("/") + "/"):
                raise ValueError("WEB_BFF_OIDC_REDIRECT_URI must use WEB_BFF_PUBLIC_ORIGIN")
        return self
