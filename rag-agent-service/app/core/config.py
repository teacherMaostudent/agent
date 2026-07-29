from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings loaded from environment variables or .env."""

    app_name: str = "rag-agent-service"
    api_prefix: str = "/api/v1"
    data_dir: Path = Path("data")
    # 持久化后端：memory(内存,重启丢,测试默认) | sqlite(落盘,重启不丢)。
    persistence: str = "memory"
    require_service_auth: bool = False
    service_api_key: str = ""
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

    agent_enabled: bool = True
    agent_model: str = "deepseek-v4-flash"
    agent_max_steps: int = Field(default=8, ge=2, le=30)
    agent_tool_timeout: float = Field(default=20.0, gt=0, le=120)
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
    context_service_base_url: str = "http://localhost:8002"
    rag_query_base_url: str = "http://localhost:8003"
    tool_gateway_base_url: str = "http://localhost:8090"
    tool_gateway_api_key: str = ""
    tool_gateway_startup_check: bool = False
    internal_service_api_key: str = ""
    service_http_timeout: float = Field(default=30.0, gt=0, le=120)
    context_max_messages: int = Field(default=12, ge=1, le=100)
    context_token_budget: int = Field(default=12000, ge=512, le=200000)
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

    model_config = SettingsConfigDict(
        env_file=".env", env_prefix="RAG_", extra="ignore"
    )

    @model_validator(mode="after")
    def validate_llm_gateway(self) -> "Settings":
        if self.llm_enabled and not self.llm_model.strip():
            raise ValueError("RAG_LLM_MODEL 不能为空")
        if (
            self.llm_enabled or self.llm_startup_check
        ) and not self.llm_gateway_base_url.startswith(("http://", "https://")):
            raise ValueError("RAG_LLM_GATEWAY_BASE_URL 必须是 http(s) 地址")
        if not self.generation_model.strip():
            raise ValueError("RAG_GENERATION_MODEL 不能为空")
        if self.rerank_provider not in {"none", "cross_encoder", "vendor"}:
            raise ValueError(
                "RAG_RERANK_PROVIDER must be none, cross_encoder, or vendor"
            )
        if self.rerank_provider == "vendor" and not self.rerank_base_url.startswith(
            ("http://", "https://")
        ):
            raise ValueError(
                "RAG_RERANK_BASE_URL must be an http(s) URL for vendor rerank"
            )
        if self.require_service_auth and not self.service_api_key:
            raise ValueError(
                "RAG_SERVICE_API_KEY is required when service auth is enabled"
            )
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
        return self.data_dir / "uploads"

    @property
    def report_dir(self) -> Path:
        return self.data_dir / "reports"

    @property
    def regulation_dir(self) -> Path:
        return self.data_dir / "regulations"

    @property
    def checklist_dir(self) -> Path:
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
        return self.data_dir / "gmp.db"

    @property
    def ingestion_jobs_path(self) -> Path:
        return self.data_dir / "ingestion_jobs.db"

    @property
    def runtime_store_path(self) -> Path:
        return self.data_dir / "runtime_runs.db"

    @property
    def runtime_checkpoint_path(self) -> Path:
        return self.data_dir / "runtime_checkpoints.db"


@lru_cache
def get_settings() -> Settings:
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
