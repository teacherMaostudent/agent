from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from platform_sdk.contracts.models import Evidence, RetrievalCandidate


class EmbeddingContract(BaseModel):
    """一个可比较向量空间的不可变身份。

    索引构建和查询必须使用相同契约；仅比较“供应商名称”不足以识别模型修订、
    维度或归一化方式的漂移。
    """

    contract_version: str = "embedding-contract/v1"
    provider: str = Field(min_length=1, max_length=80)
    model: str = Field(min_length=1, max_length=160)
    model_revision: str = Field(min_length=1, max_length=160)
    dimension: int = Field(ge=1, le=16_384)
    normalized: bool = False
    max_input_chars: int = Field(default=16_000, ge=1, le=1_000_000)
    instruction_template: str = Field(default="", max_length=4_000)
    license: str = Field(default="unspecified", max_length=160)
    deployment_mode: str = Field(default="cloud", pattern="^(cloud|self_hosted|local)$")

    @property
    def contract_id(self) -> str:
        """返回稳定摘要，用作索引映射、查询过滤与发布快照的紧凑绑定值。"""
        import hashlib
        import json

        payload = self.model_dump(mode="json")
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return f"emb_{hashlib.sha256(canonical.encode()).hexdigest()[:24]}"


class RetrievalProfilePolicy(BaseModel):
    """Snapshot-owned allow-list for RAG service-side profile enforcement."""

    policy_version: str = Field(default="retrieval-policy/v1", max_length=160)
    profile_revision: str = Field(default="retrieval-profile/v1", max_length=160)
    default_profile: str = Field(default="STANDARD", max_length=160)
    allowed_profiles: list[str] = Field(default_factory=lambda: ["STANDARD"], min_length=1)
    hard_max_rounds: int = Field(default=3, ge=0, le=100)

    def normalized_profiles(self) -> list[str]:
        """Return deterministic names and reject duplicate policy entries at publish time."""

        values = [item.upper() for item in self.allowed_profiles]
        if len(values) != len(set(values)):
            raise ValueError("retrieval policy allowed_profiles must be unique")
        if self.default_profile.upper() not in values:
            raise ValueError("retrieval policy default_profile must be allowed")
        return values


class IndexBuildManifest(BaseModel):
    """Immutable, auditable result of building one retrieval index version.

    A Release must reference a READY manifest rather than an alias name.  This
    prevents a half-built or differently embedded corpus from silently becoming
    the knowledge source for an Agent run.
    """

    manifest_id: str = Field(min_length=1, max_length=160)
    tenant_id: str = Field(min_length=1, max_length=160)
    knowledge_base: str = Field(min_length=1, max_length=160)
    index_version: str = Field(min_length=1, max_length=160)
    backend: str = Field(min_length=1, max_length=80)
    embedding_contract_id: str = Field(min_length=1, max_length=160)
    index_profile_revision: str = Field(default="hnsw-balanced/v1", max_length=160)
    chunking_revision: str = Field(default="chunker/v1", max_length=160)
    document_count: int = Field(ge=0)
    chunk_count: int = Field(ge=0)
    document_set_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    chunk_set_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: Literal["BUILDING", "READY", "FAILED", "SUPERSEDED"] = "BUILDING"
    created_at: datetime
    completed_at: datetime | None = None
    reconciliation: dict[str, Any] = Field(default_factory=dict)


class RetrievalRelease(BaseModel):
    """The complete immutable retrieval selection evaluated and released together."""

    release_id: str = Field(min_length=1, max_length=160)
    tenant_id: str = Field(min_length=1, max_length=160)
    knowledge_base: str = Field(min_length=1, max_length=160)
    index_manifest_id: str = Field(min_length=1, max_length=160)
    index_version: str = Field(min_length=1, max_length=160)
    embedding_contract_id: str = Field(min_length=1, max_length=160)
    retrieval_profile_revision: str = Field(min_length=1, max_length=160)
    reranker_contract_id: str = Field(min_length=1, max_length=160)
    fusion_revision: str = Field(default="rrf/v1", max_length=160)
    status: Literal["CANDIDATE", "SHADOW", "CANARY", "ACTIVE", "ROLLED_BACK"] = "CANDIDATE"
    created_at: datetime


class RetrievalShadowComparison(BaseModel):
    """Side-effect-free comparison of baseline and candidate retrieval releases."""

    comparison_id: str = Field(min_length=1, max_length=160)
    tenant_id: str = Field(min_length=1, max_length=160)
    baseline_release_id: str = Field(min_length=1, max_length=160)
    candidate_release_id: str = Field(min_length=1, max_length=160)
    query_hash: str = Field(min_length=1, max_length=160)
    candidate_overlap: float = Field(ge=0, le=1)
    acl_leakage_rate: float = Field(ge=0, le=1)
    baseline_latency_ms: float = Field(ge=0)
    candidate_latency_ms: float = Field(ge=0)
    baseline_cost: float = Field(ge=0)
    candidate_cost: float = Field(ge=0)
    eligible_for_canary: bool
    created_at: datetime


class RagSearchRequest(BaseModel):
    """Runtime 到 RAG Query Plane 的只读请求。

    ``top_k`` 仅保留给旧客户端兼容；已发布任务必须传入受快照约束的
    retrieval profile，由 RAG 在服务端计算实际候选与证据数量。
    """

    query: str = Field(min_length=1, max_length=4000)
    tenant_id: str = Field(default="default", max_length=160)
    user_id: str = Field(default="anonymous", max_length=160)
    document_id: str | None = Field(default=None, max_length=160)
    content: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    top_k: int = Field(default=8, ge=1, le=100)
    index_version: str = Field(default="", max_length=160)
    index_manifest_id: str = Field(default="", max_length=160)
    embedding_contract_id: str = Field(default="", max_length=80)
    retrieval_profile: str = Field(default="STANDARD", max_length=160)
    retrieval_profile_revision: str = Field(default="", max_length=160)
    reranker_contract_id: str = Field(default="", max_length=160)
    authorization_scope_digest: str = Field(default="", max_length=160)


class RagSearchResponse(BaseModel):
    query: str
    evidence: list[Evidence] = Field(default_factory=list)
    candidate_count: int = 0
    index_version: str = "local"
    embedding_contract_id: str = ""
    retrieval_profile: str = "STANDARD"
    retrieval_profile_revision: str = ""
    reranker_revision: str = ""
    candidates: list[RetrievalCandidate] = Field(default_factory=list)
    # Cache values are operational telemetry only. They never affect an
    # Evidence's verification status and are safe to show in a Runtime trace.
    cache_status: Literal["BYPASS", "MISS", "HIT_SEMANTIC"] = "BYPASS"


class RagIndexVersionResponse(BaseModel):
    """Immutable retrieval-index identity exposed to Runtime and release checks."""

    index_version: str
    backend: str
    embedding_contract: EmbeddingContract | None = None
    api_version: str = "v1"


class RagCapabilitiesResponse(BaseModel):
    """Stable discovery contract; callers must not inspect RAG implementation details."""

    api_version: str = "v1"
    operations: list[str] = Field(
        default_factory=lambda: [
            "search",
            "controlled_scan",
            "ingestion",
            "index_version",
            "health",
        ]
    )


class ControlledScanRequest(BaseModel):
    scope: str = Field(min_length=1, max_length=80)
    pattern: str = Field(min_length=1, max_length=500)
    regex: bool = False
    glob: str = Field(default="**/*", max_length=160)


class ControlledScanMatch(BaseModel):
    scope: str
    path: str
    line_number: int
    line: str


class ControlledScanResponse(BaseModel):
    scope: str
    matches: list[ControlledScanMatch] = Field(default_factory=list)
