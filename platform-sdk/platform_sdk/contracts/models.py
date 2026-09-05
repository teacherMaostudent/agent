"""Domain-neutral document and retrieval models shared by platform services."""

from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field


def new_id(prefix: str) -> str:
    """生成带领域前缀的短 UUID，用于日志关联而不是安全令牌。"""
    return f"{prefix}_{uuid4().hex[:16]}"


def utc_now() -> datetime:
    """返回带 UTC 时区的当前时间，避免跨服务持久化本地时间。"""
    return datetime.now(UTC)


class Document(BaseModel):
    document_id: str = Field(default_factory=lambda: new_id("doc"))
    filename: str
    content_type: str | None = None
    file_path: Path
    sha256: str
    status: str = "UPLOADED"
    text: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)


class Chunk(BaseModel):
    chunk_id: str = Field(default_factory=lambda: new_id("chk"))
    source_id: str
    source_type: str
    text: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class RetrievalChannel(StrEnum):
    """候选片段的召回通道；通道不是可信度，也不能直接等同于证据。"""

    DENSE = "DENSE"
    LEXICAL = "LEXICAL"
    GRAPH = "GRAPH"
    HYBRID = "HYBRID"


class RetrievalCandidate(BaseModel):
    """检索系统认为相关、但尚未获准进入模型上下文的候选片段。

    Candidate 与 Evidence 的分界是 RAG 的安全边界：候选可以携带通道分数和
    原始文本，但仍需经过租户/ACL、版本、时效、来源完整性和内容安全复核。
    """

    candidate_id: str = Field(default_factory=lambda: new_id("cand"))
    chunk_id: str = ""
    document_id: str = ""
    document_version: str = ""
    source_id: str
    source_type: str
    text: str
    score: float = 0.0
    channel: RetrievalChannel = RetrievalChannel.HYBRID
    rank: int = Field(default=0, ge=0)
    metadata: dict[str, Any] = Field(default_factory=dict)


class Evidence(BaseModel):
    """已通过证据验证器、允许投影到 Context/Prompt 的可追溯事实材料。"""

    evidence_id: str = Field(default_factory=lambda: new_id("evd"))
    source_id: str
    source_type: str
    text: str
    score: float = 0.0
    chunk_id: str = ""
    document_id: str = ""
    document_version: str = ""
    index_version: str = ""
    embedding_contract_id: str = ""
    retrieval_profile: str = ""
    retrieval_profile_revision: str = ""
    reranker_revision: str = ""
    retrieval_channels: list[RetrievalChannel] = Field(default_factory=list)
    verification_status: str = "VERIFIED"
    metadata: dict[str, Any] = Field(default_factory=dict)
