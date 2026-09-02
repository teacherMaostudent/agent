"""Runtime 独立配置：执行平面不接受或继承 RAG 服务的环境变量。"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class RuntimeSettings(BaseSettings):
    """只声明执行已发布 Agent 所需的配置，统一由 ``RUNTIME_`` 前缀提供。"""

    environment: str = "local"
    persistence: str = "memory"
    data_dir: Path = Path("data")
    database_url: str = Field(default="", repr=False)
    database_schema: str = "runtime_platform"
    contracts_schema_dir: Path = Path(__file__).parents[4] / "platform-contracts" / "schemas"
    temporal_enabled: bool = False
    temporal_target: str = "localhost:7233"
    temporal_namespace: str = "default"
    temporal_runtime_task_queue: str = "agent-runtime"
    temporal_region_targets: str = ""
    temporal_worker_region: str = ""
    temporal_worker_target_override: str = ""
    temporal_global_namespace_enabled: bool = False
    # Shadow 镜像默认关闭。开启后 Runtime 才会为已发布 Shadow Projection 异步创建独立
    # 回放 Run；生产必须在数据保留/脱敏评审完成后显式打开，不能因存在候选版本而默认复制。
    shadow_mirroring_enabled: bool = False
    context_service_base_url: str = "http://localhost:8002"
    rag_query_base_url: str = "http://localhost:8003"
    ingestion_base_url: str = "http://localhost:8004"
    tool_gateway_base_url: str = "http://localhost:8090"
    tool_gateway_api_key: str = Field(default="", repr=False)
    tool_gateway_startup_check: bool = False
    llm_gateway_base_url: str = "http://localhost:8080"
    llm_gateway_api_key: str = Field(default="", repr=False)
    llm_enabled: bool = False
    llm_startup_check: bool = False
    llm_timeout: float = 60.0
    agent_enabled: bool = True
    agent_model: str = "deepseek-v4-flash"
    agent_max_steps: int = 8
    agent_deadline_seconds: int = 60
    agent_attempt_budget: int = 6
    agent_max_cost_usd: float = 1.0
    agent_max_llm_calls: int = 8
    agent_max_tool_calls: int = 6
    agent_max_retrieval_rounds: int = 4
    agent_llm_call_reservation_usd: float = 0.01
    agent_tool_call_reservation_usd: float = 0.001
    agent_tool_timeout: float = 20.0
    agent_tool_result_max_chars: int = 12_000
    connector_heartbeat_timeout_seconds: int = Field(default=90, ge=30, le=3_600)
    connector_artifact_relay_poll_seconds: float = Field(default=2.0, ge=0.2, le=60.0)
    connector_artifact_relay_batch_size: int = Field(default=20, ge=1, le=100)
    connector_artifact_relay_lease_seconds: int = Field(default=60, ge=10, le=600)
    connector_artifact_relay_max_attempts: int = Field(default=8, ge=1, le=100)
    connector_artifact_relay_max_backoff_seconds: int = Field(default=300, ge=1, le=3_600)
    artifact_ingestion_relay_poll_seconds: float = Field(default=2.0, ge=0.2, le=60.0)
    artifact_ingestion_relay_batch_size: int = Field(default=20, ge=1, le=100)
    artifact_ingestion_relay_lease_seconds: int = Field(default=120, ge=30, le=3_600)
    artifact_ingestion_relay_max_attempts: int = Field(default=8, ge=1, le=100)
    session_archive_enabled: bool = False
    session_archive_bucket: str = ""
    session_archive_prefix: str = "agent-runtime"
    session_archive_endpoint_url: str = ""
    session_archive_region: str = ""
    session_archive_kms_key_id: str = Field(default="", repr=False)
    session_archive_retention_days: int = Field(default=365, ge=1, le=36_500)
    session_archive_compliance_mode: bool = True
    runtime_flow_version: int = 1
    snapshot_required: bool = False
    executor_catalog_version: str = "runtime-executor-catalog/v1"
    capability_catalog_version: str = "runtime-capability-catalog/v1"
    code_runner_enabled: bool = False
    control_plane_base_url: str = ""
    control_plane_runtime_key: str = Field(default="", repr=False)
    governance_base_url: str = ""
    governance_event_key: str = Field(default="", repr=False)
    governance_delivery_mode: str = "direct"
    internal_service_api_key: str = Field(default="", repr=False)
    service_http_timeout: float = 30.0
    workload_token_url: str = ""
    workload_client_id: str = "agent-runtime"
    workload_client_secret: str = Field(default="", repr=False)
    workload_audience: str = "agent-platform"
    workload_scope: str = ""
    mtls_enabled: bool = False
    mtls_ca_file: str = ""
    mtls_cert_file: str = ""
    mtls_key_file: str = Field(default="", repr=False)
    api_prefix: str = "/api/v1"
    cors_origins: list[str] = []
    require_service_auth: bool = False
    service_api_key: str = Field(default="", repr=False)
    # API 根地址仅返回浏览器说明页。使用精确匹配，禁止顺带豁免 /api/v1 下的业务接口。
    service_auth_exempt_paths: list[str] = ["/api/v1"]
    # 用户交互入口由 OIDC/OPA 鉴权；内部能力、目录和运维接口仍要求工作负载凭据。
    # 生产配置校验强制启用 OIDC，因此该豁免不会把裸身份 Header 变成生产信任根。
    service_auth_exempt_prefixes: list[str] = ["/api/v1/agent"]
    oidc_enabled: bool = False
    oidc_issuer: str = ""
    oidc_audience: str = "agent-platform"
    oidc_jwks_url: str = ""
    opa_enabled: bool = False
    opa_base_url: str = "http://localhost:8181"
    opa_decision_path: str = "agent_platform/allow"

    # 执行平面已完成与检索应用的拆分. 接受旧前缀会让部署配置重新形成隐式耦合。
    model_config = SettingsConfigDict(env_file=".env", env_prefix="RUNTIME_", extra="ignore")

    @model_validator(mode="after")
    def validate_production_boundary(self) -> RuntimeSettings:
        """生产启动时强制独立执行平面的持久化、身份与调度边界，禁止隐式本地降级。"""
        if self.environment.lower() not in {"production", "prod"}:
            return self
        unsafe: list[str] = []
        if self.persistence != "postgres" or not self.database_url:
            unsafe.append("RUNTIME_PERSISTENCE must be postgres and DATABASE_URL is required")
        if not self.snapshot_required:
            unsafe.append("RUNTIME_SNAPSHOT_REQUIRED must be true")
        if not self.temporal_enabled:
            unsafe.append("RUNTIME_TEMPORAL_ENABLED must be true")
        if self.temporal_region_targets and not self.temporal_global_namespace_enabled:
            unsafe.append(
                "RUNTIME_TEMPORAL_GLOBAL_NAMESPACE_ENABLED must be true for cross-cluster targets"
            )
        if not self.oidc_enabled or not self.oidc_issuer or not self.oidc_jwks_url:
            unsafe.append("RUNTIME OIDC issuer and JWKS configuration are required")
        if not self.workload_token_url or not self.workload_client_secret:
            unsafe.append("RUNTIME workload client credentials are required")
        if not self.require_service_auth or not self.service_api_key:
            unsafe.append("RUNTIME service authentication must be enabled")
        if not self.mtls_enabled or not all(
            (self.mtls_ca_file, self.mtls_cert_file, self.mtls_key_file)
        ):
            unsafe.append("RUNTIME mTLS certificate paths are required")
        if not self.control_plane_base_url or not self.governance_base_url:
            unsafe.append("RUNTIME Control Plane and Governance endpoints are required")
        if self.session_archive_enabled and not self.session_archive_bucket:
            unsafe.append(
                "RUNTIME_SESSION_ARCHIVE_BUCKET is required when session archiving is enabled"
            )
        if unsafe:
            raise ValueError("Unsafe production configuration: " + "; ".join(unsafe))
        return self

    @property
    def runtime_store_path(self) -> Path:
        """返回开发 SQLite Run/Outbox 路径；生产 PostgreSQL 不使用该文件。"""
        return self.data_dir / "runtime_runs.db"

    @property
    def runtime_checkpoint_path(self) -> Path:
        """返回开发 LangGraph 检查点路径；不可用于多副本共享状态。"""
        return self.data_dir / "runtime_checkpoints.db"

    @property
    def runtime_jobs_path(self) -> Path:
        """返回开发异步队列路径；生产调度改用 Temporal。"""
        return self.data_dir / "runtime_jobs.db"


@lru_cache
def get_settings() -> RuntimeSettings:
    """缓存一次配置并确保本地数据目录存在，避免每个请求重复解析环境变量。"""
    settings = RuntimeSettings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    return settings
