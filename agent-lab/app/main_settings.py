"""Agent Lab 运行配置：生产模式只接受持久化、身份和隔离均完整的部署声明。"""

from __future__ import annotations

import socket
from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """声明 API 与 Worker 共用配置，避免同一实验在两类进程使用不同安全边界。"""

    model_config = SettingsConfigDict(env_prefix="AGENT_LAB_", env_file=".env", extra="ignore")
    environment: str = "local"
    database_backend: str = "sqlite"
    database_path: Path = Path("./data/agent-lab.db")
    database_url: str = Field(default="", repr=False)
    database_schema: str = "agent_lab"
    control_plane_base_url: str = "http://localhost:8082"
    runtime_base_url: str = "http://localhost:8001"
    governance_base_url: str = "http://localhost:8081"
    control_plane_runtime_key: str = Field(default="", repr=False)
    governance_auditor_key: str = Field(default="", repr=False)
    service_api_key: str = Field(default="", repr=False)
    max_cases: int = Field(default=50, ge=1, le=200)
    request_timeout_seconds: float = Field(default=120.0, gt=0, le=3600)
    temporal_enabled: bool = False
    temporal_target: str = "localhost:7233"
    temporal_namespace: str = "default"
    temporal_task_queue: str = "agent-lab-experiments"
    worker_id: str = Field(default_factory=lambda: f"agent-lab-{socket.gethostname()}")
    job_lease_seconds: int = Field(default=900, ge=30, le=7200)
    job_max_attempts: int = Field(default=5, ge=1, le=20)
    retry_initial_seconds: int = Field(default=5, ge=1, le=300)
    retry_max_seconds: int = Field(default=300, ge=1, le=3600)
    oidc_enabled: bool = False
    oidc_issuer: str = ""
    oidc_audience: str = "agent-platform"
    oidc_jwks_url: str = ""
    workload_token_url: str = ""
    workload_client_id: str = "agent-lab"
    workload_client_secret: str = Field(default="", repr=False)
    workload_audience: str = "agent-platform"
    workload_scope: str = ""
    mtls_enabled: bool = False
    mtls_ca_file: str = ""
    mtls_cert_file: str = ""
    mtls_key_file: str = Field(default="", repr=False)
    sandbox_provider: str = "docker"
    # JSON list in environment variables, e.g. ["python:3.12-slim","node:22-alpine"].
    # Exact matching prevents a submitted experiment from selecting arbitrary images.
    sandbox_image_allowlist: list[str] = Field(default_factory=lambda: ["python:3.12-slim"])

    @model_validator(mode="after")
    def validate_production_boundary(self) -> Settings:
        """生产启动前拒绝同步 SQLite、无身份或无 Temporal 的伪生产实验平台。"""
        if self.environment.lower() not in {"production", "prod"}:
            return self
        unsafe: list[str] = []
        if self.database_backend != "postgres" or not self.database_url:
            unsafe.append("AGENT_LAB_DATABASE_BACKEND must be postgres")
        if not self.temporal_enabled:
            unsafe.append("AGENT_LAB_TEMPORAL_ENABLED must be true")
        if not self.oidc_enabled or not self.oidc_issuer or not self.oidc_jwks_url:
            unsafe.append("AGENT_LAB OIDC issuer and JWKS configuration are required")
        if not self.workload_token_url or not self.workload_client_secret:
            unsafe.append("AGENT_LAB workload client credentials are required")
        if not self.mtls_enabled or not all(
            (self.mtls_ca_file, self.mtls_cert_file, self.mtls_key_file)
        ):
            unsafe.append("AGENT_LAB mTLS certificate paths are required")
        if not self.service_api_key:
            unsafe.append(
                "AGENT_LAB_SERVICE_API_KEY is required for Control Plane release evidence"
            )
        if not self.sandbox_image_allowlist:
            unsafe.append("AGENT_LAB_SANDBOX_IMAGE_ALLOWLIST must not be empty")
        if unsafe:
            raise ValueError("Unsafe production configuration: " + "; ".join(unsafe))
        return self
