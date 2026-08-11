"""Domain-neutral document and retrieval models shared by platform services."""

from datetime import UTC, datetime
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


class Evidence(BaseModel):
    source_id: str
    source_type: str
    text: str
    score: float = 0.0
    metadata: dict[str, Any] = Field(default_factory=dict)
