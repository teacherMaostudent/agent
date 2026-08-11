from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings loaded from environment variables or .env."""

    app_name: str = "rag-agent-service"
    deployment_environment: str = "local"
    api_prefix: str = "/api/v1"
    data_dir: Path = Path("data")
    # 持久化后端：memory(内存,重启丢,测试默认) | sqlite(落盘,重启不丢)。
    persistence: str = "memory"
    database_url: str = Field(default="", repr=False)
    database_schema: str = "rag_platform"
    temporal_enabled: bool = False
    temporal_target: str = "localhost:7233"
    temporal_region_targets: str = ""
    temporal_namespace: str = "default"
    temporal_runtime_task_queue: str = "agent-runtime"
    temporal_worker_region: str = ""
    temporal_ingestion_task_queue: str = "rag-ingestion"
    temporal_execution_timeout_seconds: int = Field(default=3600, ge=60)
    object_storage_backend: str = "local"
    s3_bucket: str = ""
    s3_prefix: str = "agent-platform"
    s3_endpoint_url: str = ""
    s3_region: str = ""
    s3_kms_key_id: str = Field(default="", repr=False)
    search_backend: str = "local"
    opensearch_url: str = ""
    opensearch_username: str = ""
    opensearch_password: str = Field(default="", repr=False)
    opensearch_index_alias: str = "agent-knowledge-current"
    opensearch_index_version: str = "v1"
    oidc_enabled: bool = False
    oidc_issuer: str = ""
    oidc_audience: str = "agent-platform"
    oidc_jwks_url: str = ""
    oidc_permissions_claim: str = "permissions"
    workload_token_url: str = ""
    workload_client_id: str = "rag-platform"
    workload_client_secret: str = Field(default="", repr=False)
    workload_audience: str = "agent-platform"
    workload_scope: str = ""
    opa_enabled: bool = False
    opa_base_url: str = "http://localhost:8181"
    opa_decision_path: str = "agent_platform/allow"
    redis_url: str = Field(default="", repr=False)
    require_service_auth: bool = False
    service_api_key: str = ""
    allow_legacy_public_documents: bool = False
    # 所有聊天模型统一经过 llm-gateway。Python 服务只使用网关逻辑模型名，
    # 厂家地址、厂家密钥、路由和 fallback 均由 Java 网关管理。
    llm_gateway_base_url: str = "http://localhost:8080"
    llm_gateway_api_key: str = ""
    llm_gateway_user_id: str = "rag-agent-service"
    local_embedding_dim: int = 384
    bm25_weight: float = 0.55
    vector_weight: float = 0.45
    top_k: int = 8
    retrieval_candidate_k: int = Field(default=32, ge=4, le=200)
    rerank_provider: str = "none"  # none | cross_encoder | vendor
    rerank_model: str = "BAAI/bge-reranker-v2-m3"
    rerank_base_url: str = ""
    rerank_api_key: str = ""
    rerank_timeout: float = 15.0
    rerank_batch_size: int = Field(default=16, ge=1, le=128)
    scan_roots: dict[str, str] = Field(default_factory=dict)
    scan_max_file_bytes: int = Field(default=2_000_000, ge=1_024, le=50_000_000)
    scan_max_files: int = Field(default=200, ge=1, le=10_000)
    scan_max_results: int = Field(default=200, ge=1, le=10_000)

    agent_enabled: bool = True
    agent_model: str = "deepseek-v4-flash"
    agent_max_steps: int = Field(default=8, ge=2, le=30)
    agent_tool_timeout: float = Field(default=20.0, gt=0, le=120)
    agent_tool_result_max_chars: int = Field(default=12_000, ge=512, le=200_000)
    agent_deadline_seconds: int = Field(default=60, ge=1, le=600)
    agent_attempt_budget: int = Field(default=6, ge=0, le=100)
    agent_max_cost_usd: float = Field(default=1.0, gt=0, le=10_000)
    agent_max_llm_calls: int = Field(default=8, ge=0, le=100)
    agent_max_tool_calls: int = Field(default=6, ge=0, le=100)
    agent_max_retrieval_rounds: int = Field(default=4, ge=0, le=100)
    agent_llm_call_reservation_usd: float = Field(default=0.01, ge=0, le=100)
    agent_tool_call_reservation_usd: float = Field(default=0.001, ge=0, le=100)
    runtime_flow_version: int = Field(default=1, ge=1, le=1_000)
    runtime_snapshot_required: bool = False
    control_plane_base_url: str = ""
    control_plane_runtime_key: str = ""
    governance_base_url: str = ""
    governance_event_key: str = ""
    governance_delivery_mode: str = "direct"
    context_service_base_url: str = "http://localhost:8002"
    rag_query_base_url: str = "http://localhost:8003"
    tool_gateway_base_url: str = "http://localhost:8090"
    tool_gateway_api_key: str = ""
    tool_gateway_startup_check: bool = False
    internal_service_api_key: str = ""
    mtls_enabled: bool = False
    mtls_ca_file: str = ""
    mtls_cert_file: str = ""
    mtls_key_file: str = Field(default="", repr=False)
    service_http_timeout: float = Field(default=30.0, gt=0, le=120)
    context_max_messages: int = Field(default=12, ge=1, le=100)
    context_max_stored_messages: int = Field(default=500, ge=10, le=100_000)
    context_retention_days: int = Field(default=30, ge=1, le=3_650)
    context_token_budget: int = Field(default=12000, ge=512, le=200000)
    context_message_budget_ratio: float = Field(default=0.4, ge=0.1, le=0.9)
    ingestion_poll_interval: float = Field(default=1.0, ge=0.1, le=60)

    otel_enabled: bool = False
    otel_service_name: str = "rag-agent-service"
    otel_exporter_endpoint: str = "http://localhost:4318/v1/traces"
    otel_environment: str = "local"
    # 阶段 B：是否用大模型做语义判定(覆盖率/数据可靠性)。
    # 默认关闭以支持离线开发；生产环境应显式打开并启用启动连通性检查。
    llm_enabled: bool = False
    llm_model: str = "deepseek-v4-flash"
    llm_timeout: float = 60.0
    llm_startup_check: bool = False
    llm_batch_size: int = Field(default=8, ge=1, le=20)
    generation_model: str = "deepseek-v4-flash"
    generation_timeout: float = 120.0

    # 法规库 / embedding：provider=hash(默认,离线免密钥) | qwen(真实语义)。
    embedding_provider: str = "hash"
    # 通义密钥(sk-开头)，聊天与 embedding 共用。同时接受裸 DASHSCOPE_API_KEY
    # 和带前缀 RAG_DASHSCOPE_API_KEY，用户已有的裸变量可直接用。
    dashscope_api_key: str = Field(
        default="",
        validation_alias=AliasChoices("DASHSCOPE_API_KEY", "RAG_DASHSCOPE_API_KEY"),
    )
    qwen_embedding_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    qwen_embedding_model: str = "text-embedding-v3"
    qwen_embedding_batch_size: int = 10
    # 法规原件 PDF 所在目录(默认桌面项目根)；建库时从这里读取纳入的 5 部法规。
    regulation_source_dir: Path = Path("..")
    # 业务前端跨域来源。默认 ["*"] 全放行(开发)；部署改为具体域名列表。
    cors_origins: list[str] = ["*"]

    model_config = SettingsConfigDict(env_file=".env", env_prefix="RAG_", extra="ignore")

    @model_validator(mode="after")
    def validate_llm_gateway(self) -> "Settings":
        """拒绝不满足生产边界的组合配置，避免服务以不安全的隐式降级方式启动。"""
        if self.temporal_enabled and self.persistence != "postgres":
            raise ValueError(
                "RAG_TEMPORAL_ENABLED requires RAG_PERSISTENCE=postgres so workflow "
                "activities and API replicas share the same durable job/run state"
            )
        if self.llm_enabled and not self.llm_model.strip():
            raise ValueError("RAG_LLM_MODEL 不能为空")
        if (
            self.llm_enabled or self.llm_startup_check
        ) and not self.llm_gateway_base_url.startswith(("http://", "https://")):
            raise ValueError("RAG_LLM_GATEWAY_BASE_URL 必须是 http(s) 地址")
        if not self.generation_model.strip():
            raise ValueError("RAG_GENERATION_MODEL 不能为空")
        if self.rerank_provider not in {"none", "cross_encoder", "vendor"}:
            raise ValueError("RAG_RERANK_PROVIDER must be none, cross_encoder, or vendor")
        if self.rerank_provider == "vendor" and not self.rerank_base_url.startswith(
            ("http://", "https://")
        ):
            raise ValueError("RAG_RERANK_BASE_URL must be an http(s) URL for vendor rerank")
        if self.require_service_auth and not self.service_api_key:
            raise ValueError("RAG_SERVICE_API_KEY is required when service auth is enabled")
        if self.deployment_environment.lower() in {"production", "prod"}:
            unsafe: list[str] = []
            if not self.require_service_auth:
                unsafe.append("RAG_REQUIRE_SERVICE_AUTH must be true")
            if "*" in self.cors_origins:
                unsafe.append("RAG_CORS_ORIGINS must not contain '*'")
            if self.persistence != "postgres" or not self.database_url:
                unsafe.append("RAG_PERSISTENCE must be postgres and DATABASE_URL is required")
            if not self.runtime_snapshot_required:
                unsafe.append("RAG_RUNTIME_SNAPSHOT_REQUIRED must be true")
            if self.allow_legacy_public_documents:
                unsafe.append("RAG_ALLOW_LEGACY_PUBLIC_DOCUMENTS must be false")
            if not self.temporal_enabled:
                unsafe.append("RAG_TEMPORAL_ENABLED must be true")
            if self.object_storage_backend != "s3" or not self.s3_bucket:
                unsafe.append("RAG_OBJECT_STORAGE_BACKEND must be s3 and S3_BUCKET is required")
            if self.search_backend != "opensearch" or not self.opensearch_url:
                unsafe.append("RAG_SEARCH_BACKEND must be opensearch")
            if not self.oidc_enabled or not self.oidc_issuer or not self.oidc_jwks_url:
                unsafe.append("RAG_OIDC_ENABLED, OIDC_ISSUER and OIDC_JWKS_URL are required")
            if not self.oidc_permissions_claim.strip():
                unsafe.append("RAG_OIDC_PERMISSIONS_CLAIM is required")
            if not self.workload_token_url or not self.workload_client_secret:
                unsafe.append("RAG_WORKLOAD_TOKEN_URL and WORKLOAD_CLIENT_SECRET are required")
            if not self.opa_enabled:
                unsafe.append("RAG_OPA_ENABLED must be true")
            if not self.redis_url:
                unsafe.append("RAG_REDIS_URL is required")
            if self.governance_delivery_mode != "cdc":
                unsafe.append("RAG_GOVERNANCE_DELIVERY_MODE must be cdc")
            if self.mtls_enabled and not all(
                (self.mtls_ca_file, self.mtls_cert_file, self.mtls_key_file)
            ):
                unsafe.append("RAG mTLS certificate paths are required")
            if unsafe:
                raise ValueError("Unsafe production configuration: " + "; ".join(unsafe))
        if self.governance_delivery_mode not in {"direct", "cdc"}:
            raise ValueError("RAG_GOVERNANCE_DELIVERY_MODE must be direct or cdc")
        for name, value in {
            "RAG_CONTEXT_SERVICE_BASE_URL": self.context_service_base_url,
            "RAG_RAG_QUERY_BASE_URL": self.rag_query_base_url,
            "RAG_TOOL_GATEWAY_BASE_URL": self.tool_gateway_base_url,
        }.items():
            if not value.startswith(("http://", "https://")):
                raise ValueError(f"{name} must be an http(s) URL")
        return self

    @property
    def upload_dir(self) -> Path:
        """返回上传原件的本地暂存目录；生产权威副本位于对象存储。"""
        return self.data_dir / "uploads"

    @property
    def report_dir(self) -> Path:
        """返回报告缓存目录；不可变归档由对象存储或 WORM 存储负责。"""
        return self.data_dir / "reports"

    @property
    def regulation_dir(self) -> Path:
        """返回本地检索素材目录，仅供开发或可重建缓存使用。"""
        return self.data_dir / "regulations"

    @property
    def checklist_dir(self) -> Path:
        """返回检查清单的本地目录；业务发布不应依赖未版本化的本地文件。"""
        return self.data_dir / "checklists"

    @property
    def regulation_store_path(self) -> Path:
        """法规库向量缓存文件(建库一次写入，重启加载不重算)。"""
        return self.regulation_dir / "regulation_store.json"

    @property
    def snapshot_dir(self) -> Path:
        """跨文档审查快照目录(每次审查冻结一份，含人工标注，重启不丢)。"""
        return self.data_dir / "cross_snapshots"

    @property
    def sqlite_path(self) -> Path:
        """业务数据 SQLite 文件(文档元数据 + 审查报告，重启不丢)。向量不进这里。"""
        return self.data_dir / "rag-agent.db"

    @property
    def ingestion_jobs_path(self) -> Path:
        """返回 SQLite 开发队列路径；生产任务状态必须使用 PostgreSQL/Temporal。"""
        return self.data_dir / "ingestion_jobs.db"

    @property
    def runtime_store_path(self) -> Path:
        """保留迁移兼容的 Runtime 状态路径；独立 Runtime 服务不应写入此路径。"""
        return self.data_dir / "runtime_runs.db"

    @property
    def runtime_checkpoint_path(self) -> Path:
        """保留迁移兼容的检查点路径；生产执行检查点属于 Runtime 自身存储。"""
        return self.data_dir / "runtime_checkpoints.db"

    @property
    def runtime_jobs_path(self) -> Path:
        """保留迁移兼容的任务路径；RAG 摄取流程不使用 Runtime 队列。"""
        return self.data_dir / "runtime_jobs.db"


@lru_cache
def get_settings() -> Settings:
    """缓存并初始化目录配置；只在进程启动阶段调用，避免运行中读取环境漂移。"""
    settings = Settings()
    for path in [
        settings.data_dir,
        settings.upload_dir,
        settings.report_dir,
        settings.regulation_dir,
        settings.checklist_dir,
        settings.snapshot_dir,
    ]:
        path.mkdir(parents=True, exist_ok=True)
    return settings
