"""Context Service 所有的不可变 Task Artifact 元数据存储。"""

from __future__ import annotations

from threading import RLock
from uuid import uuid4

from platform_sdk.contracts.artifacts import TaskArtifact, TaskArtifactCreate

_KIND = "task_artifact"


class TaskArtifactStore:
    """保存中间成果引用和摘要；正文继续位于对象存储或原业务数据域。"""

    def __init__(self, backend=None) -> None:
        """复用 Context 的 PostgreSQL/SQLite KV 后端，并保留内存测试模式。"""
        self._db = backend
        self._memory: dict[str, TaskArtifact] = {}
        self._lock = RLock()

    def create(self, tenant_id: str, user_id: str, request: TaskArtifactCreate) -> TaskArtifact:
        """创建不可变引用；服务生成 ID，调用方不能覆盖已有工件。"""
        artifact = TaskArtifact(
            artifact_id=f"art_{uuid4().hex}",
            tenant_id=tenant_id,
            root_task_id=request.root_task_id,
            artifact_type=request.artifact_type,
            content_ref=request.content_ref,
            content_sha256=request.content_sha256,
            media_type=request.media_type,
            metadata=request.metadata,
            created_by=user_id,
        )
        key = self._key(tenant_id, request.root_task_id, artifact.artifact_id)
        with self._lock:
            if self._db is not None:
                if not self._db.put_if_version(_KIND, key, artifact.model_dump(mode="json"), 0):
                    raise RuntimeError("task artifact ID collision")
            else:
                self._memory[key] = artifact
        return artifact

    def get(self, tenant_id: str, root_task_id: str, artifact_id: str) -> TaskArtifact | None:
        """按 tenant/root/artifact 三元键读取，防止猜测 ID 跨任务访问。"""
        key = self._key(tenant_id, root_task_id, artifact_id)
        with self._lock:
            if self._db is not None:
                payload = self._db.get(_KIND, key)
                return TaskArtifact.model_validate(payload) if payload else None
            return self._memory.get(key)

    @staticmethod
    def _key(tenant_id: str, root_task_id: str, artifact_id: str) -> str:
        """构造包含全部隔离维度的内部 KV 键。"""
        return f"{tenant_id}:{root_task_id}:{artifact_id}"
