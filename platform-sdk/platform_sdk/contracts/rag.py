from typing import Any

from pydantic import BaseModel, Field

from platform_sdk.contracts.models import Evidence


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


class RagSearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=4000)
    tenant_id: str = Field(default="default", max_length=160)
    user_id: str = Field(default="anonymous", max_length=160)
    document_id: str | None = Field(default=None, max_length=160)
    content: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    top_k: int = Field(default=8, ge=1, le=100)
    index_version: str = Field(default="", max_length=160)
    embedding_contract_id: str = Field(default="", max_length=80)


class RagSearchResponse(BaseModel):
    query: str
    evidence: list[Evidence] = Field(default_factory=list)
    candidate_count: int = 0
    index_version: str = "local"
    embedding_contract_id: str = ""


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
