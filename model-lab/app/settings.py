"""Model Lab 的生产配置边界。"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """固定实验元数据、身份和工件边界，避免部署通过任意环境变量削弱证据链。"""

    model_config = SettingsConfigDict(env_prefix="MODEL_LAB_", env_file=".env", extra="ignore")
    environment: str = "local"
    database_backend: str = "sqlite"
    database_path: Path = Path("data/model-lab.db")
    database_url: str = Field(default="", repr=False)
    database_schema: str = "model_lab"
    service_api_key: str = Field(default="", repr=False)
    artifact_bucket: str = ""
    oidc_enabled: bool = False
    oidc_issuer: str = ""
    oidc_audience: str = "agent-platform"
    oidc_jwks_url: str = ""
    mtls_enabled: bool = False
    mtls_ca_file: str = ""
    mtls_cert_file: str = ""
    mtls_key_file: str = Field(default="", repr=False)
    otel_enabled: bool = False
    otel_endpoint: str = ""

    @model_validator(mode="after")
    def validate_production_boundary(self) -> Settings:
        """生产拒绝 SQLite、匿名调用、未声明工件桶或缺失服务端 mTLS 的组合。"""
        if self.environment.lower() not in {"production", "prod"}:
            return self
        unsafe: list[str] = []
        if self.database_backend != "postgres" or not self.database_url:
            unsafe.append("MODEL_LAB_DATABASE_BACKEND must be postgres with DATABASE_URL")
        if not self.artifact_bucket:
            unsafe.append("MODEL_LAB_ARTIFACT_BUCKET is required")
        if not self.service_api_key:
            unsafe.append("MODEL_LAB_SERVICE_API_KEY is required")
        if not self.oidc_enabled or not self.oidc_issuer or not self.oidc_jwks_url:
            unsafe.append("MODEL_LAB OIDC issuer and JWKS configuration are required")
        if not self.mtls_enabled or not all((self.mtls_ca_file, self.mtls_cert_file, self.mtls_key_file)):
            unsafe.append("MODEL_LAB mTLS certificate paths are required")
        if unsafe:
            raise ValueError("Unsafe production configuration: " + "; ".join(unsafe))
        return self
