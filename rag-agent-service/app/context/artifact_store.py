"""Context Service 所有的不可变 Task Artifact 元数据存储。"""

from __future__ import annotations

from threading import RLock
from uuid import uuid4

from platform_sdk.contracts.artifacts import TaskArtifact, TaskArtifactCreate

_KIND = "task_artifact"
_SERIES_KIND = "task_artifact_series"


class TaskArtifactStore:
    """保存中间成果引用和摘要；正文继续位于对象存储或原业务数据域。"""

    def __init__(self, backend=None) -> None:
        """复用 Context 的 PostgreSQL/SQLite KV 后端，并保留内存测试模式。"""
        self._db = backend
        self._memory: dict[str, TaskArtifact] = {}
        self._lock = RLock()

    def create(self, tenant_id: str, user_id: str, request: TaskArtifactCreate) -> TaskArtifact:
        """创建不可变引用；服务生成 ID，调用方不能覆盖已有工件。"""
        artifact_id = f"art_{uuid4().hex}"
        logical_name = request.logical_name.strip() or request.artifact_type
        with self._lock:
            # Keep allocation and insert atomic in memory mode. In distributed mode the
            # series-head CAS below supplies the cross-replica ordering guarantee.
            version, previous_artifact_id = self._allocate_version(
                tenant_id, request.root_task_id, logical_name, artifact_id
            )
            artifact = TaskArtifact(
                artifact_id=artifact_id,
                tenant_id=tenant_id,
                root_task_id=request.root_task_id,
                artifact_type=request.artifact_type,
                content_ref=request.content_ref,
                content_sha256=request.content_sha256,
                media_type=request.media_type,
                logical_name=logical_name,
                version=version,
                previous_artifact_id=previous_artifact_id,
                metadata=request.metadata,
                created_by=user_id,
            )
            key = self._key(tenant_id, request.root_task_id, artifact.artifact_id)
            if self._db is not None:
                if not self._db.put_if_version(_KIND, key, artifact.model_dump(mode="json"), 0):
                    raise RuntimeError("task artifact ID collision")
            else:
                self._memory[key] = artifact
        return artifact

    def _allocate_version(
        self,
        tenant_id: str,
        root_task_id: str,
        logical_name: str,
        artifact_id: str,
    ) -> tuple[int, str | None]:
        """CAS-allocate a monotonic version across concurrent Context replicas.

        The series head is a separate coordination record because Task Artifacts remain
        immutable. A failed create can leave a version gap, which is auditable and safer than
        assigning a duplicate version under concurrency.
        """
        series_key = f"{tenant_id}:{root_task_id}:{logical_name}"
        with self._lock:
            if self._db is None:
                siblings = [
                    item
                    for item in self._memory.values()
                    if item.tenant_id == tenant_id
                    and item.root_task_id == root_task_id
                    and (item.logical_name or item.artifact_type) == logical_name
                ]
                latest = max(siblings, key=lambda item: item.version, default=None)
                return (latest.version + 1, latest.artifact_id) if latest else (1, None)
            for _ in range(8):
                current, revision = self._db.get_with_version(_SERIES_KIND, series_key)
                next_version = int(current.get("version", 0)) + 1 if current else 1
                previous = (str(current.get("artifact_id", "")) or None) if current else None
                if self._db.put_if_version(
                    _SERIES_KIND,
                    series_key,
                    {"version": next_version, "artifact_id": artifact_id},
                    revision,
                ):
                    return next_version, previous
        raise RuntimeError("artifact version allocation conflict")

    def get(self, tenant_id: str, root_task_id: str, artifact_id: str) -> TaskArtifact | None:
        """按 tenant/root/artifact 三元键读取，防止猜测 ID 跨任务访问。"""
        key = self._key(tenant_id, root_task_id, artifact_id)
        with self._lock:
            if self._db is not None:
                payload = self._db.get(_KIND, key)
                return TaskArtifact.model_validate(payload) if payload else None
            return self._memory.get(key)

    def list(self, tenant_id: str, root_task_id: str, *, limit: int = 100) -> list[TaskArtifact]:
        """列出一个 RootTask 的工件索引，不按租户扫描或返回工件正文。"""
        bounded_limit = min(max(limit, 1), 200)
        prefix = f"{tenant_id}:{root_task_id}:"
        with self._lock:
            if self._db is not None:
                # KV 后端没有业务查询能力时只读取同一 RootTask 前缀；调用方仍须先在
                # Runtime 完成 Run 资源授权，Context 不承担浏览器级共享策略。
                items = self._db.list_prefix(_KIND, prefix, limit=bounded_limit)
                artifacts = [TaskArtifact.model_validate(item) for item in items]
            else:
                artifacts = [item for key, item in self._memory.items() if key.startswith(prefix)]
        return sorted(artifacts, key=lambda item: item.created_at, reverse=True)[:bounded_limit]

    @staticmethod
    def _key(tenant_id: str, root_task_id: str, artifact_id: str) -> str:
        """构造包含全部隔离维度的内部 KV 键。"""
        return f"{tenant_id}:{root_task_id}:{artifact_id}"
