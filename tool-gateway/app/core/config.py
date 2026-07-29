from functools import lru_cache
from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "tool-gateway"
    api_prefix: str = "/api/v1"
    host: str = "0.0.0.0"
    port: int = Field(default=8090, ge=1, le=65_535)
    database_path: Path = Path("data/tool-gateway.db")
    tools_config_path: Path = Path("config/tools.json")
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
        return self

    def ensure_directories(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_directories()
    return settings
