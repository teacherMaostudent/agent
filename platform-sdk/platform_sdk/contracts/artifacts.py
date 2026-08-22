"""RootTask 共享中间成果的不可变引用契约。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field


class TaskArtifactCreate(BaseModel):
    """创建不可变 Task Artifact；大正文使用 content_ref，不在组件间复制。"""

    root_task_id: str = Field(min_length=1, max_length=160)
    artifact_type: str = Field(min_length=1, max_length=100)
    content_ref: str = Field(min_length=1, max_length=2_000)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    media_type: str = Field(default="application/json", max_length=160)
    logical_name: str = Field(default="", max_length=160)
    metadata: dict[str, Any] = Field(default_factory=dict)


class TaskArtifactTextCreate(BaseModel):
    """由受信任服务写入小型文本交付物；大文件仍应走专用摄取/对象存储流程。"""

    content: str = Field(min_length=1, max_length=200_000)
    artifact_type: str = Field(default="final-report", min_length=1, max_length=100)
    media_type: str = Field(default="text/markdown; charset=utf-8", max_length=160)
    logical_name: str = Field(default="", max_length=160)


class TaskArtifact(BaseModel):
    """可跨 Workflow、Agent 与 Skill 传递的不可变、租户隔离引用。"""

    artifact_id: str
    tenant_id: str
    root_task_id: str
    artifact_type: str
    content_ref: str
    content_sha256: str
    media_type: str
    logical_name: str = ""
    version: int = Field(default=1, ge=1)
    previous_artifact_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_by: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class TaskArtifactPreview(BaseModel):
    """Bounded text projection; binary objects never pass through the application API."""

    artifact_id: str
    logical_name: str
    version: int
    media_type: str
    content: str
    truncated: bool
    content_sha256: str
    sha256_verified: bool | None = None


class TaskArtifactComparison(BaseModel):
    """Bounded unified diff between two versions in the same artifact series."""

    base_artifact_id: str
    target_artifact_id: str
    logical_name: str
    base_version: int
    target_version: int
    diff: str
    truncated: bool
