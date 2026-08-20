"""Transactional repository reference implementation for Control Plane.

SQLite is kept for local development and tests.  Its transaction boundaries
mirror the PostgreSQL adapter: state change and outbox insert occur together so
downstream consumers can retry delivery without losing the release fact.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, TypeVar

from app.domain.models import (
    AgentDefinition,
    AgentVersion,
    OutboxEvent,
    ReleaseManifest,
    ReleaseStatus,
    SkillDefinition,
    SkillStatus,
    SkillVersion,
    TenantPolicy,
    WorkflowDefinition,
    WorkflowRelease,
    WorkflowVersion,
)

T = TypeVar("T")


class ControlPlaneRepositoryOperations:
    """与数据库方言无关的 Control Plane 聚合操作；I/O 事务由子类提供。"""

    def __init__(self, database_path: Path, schema_path: Path) -> None:
        """初始化该组件的依赖、配置与内部状态。


        Initialize SqliteRepository dependencies and local state.
        """
        self._database_path = database_path
        self._schema_path = schema_path
        self._write_lock = asyncio.Lock()

    async def initialize(self) -> None:
        """处理 initialize 对应的当前组件内部业务步骤。


        Perform initialize within the SqliteRepository ownership boundary.
        """
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        schema = self._schema_path.read_text(encoding="utf-8")

        def operation() -> None:
            """在独立数据库线程中执行本次受控操作，连接与事务边界由外层读写助手统一管理。"""
            with self._connect() as connection:
                connection.executescript(schema)
                columns = {row["name"] for row in connection.execute("PRAGMA table_info(releases)")}
                if "quality_gate_id" not in columns:
                    connection.execute("ALTER TABLE releases ADD COLUMN quality_gate_id TEXT")
                if "quality_gate_metrics_json" not in columns:
                    connection.execute(
                        "ALTER TABLE releases ADD COLUMN quality_gate_metrics_json "
                        "TEXT NOT NULL DEFAULT '{}'"
                    )
                if "agent_lab_experiment_id" not in columns:
                    connection.execute(
                        "ALTER TABLE releases ADD COLUMN agent_lab_experiment_id TEXT"
                    )
                for column in (
                    "runtime_executor_catalog_version",
                    "runtime_executor_cluster_id",
                    "runtime_executor_catalog_hash",
                    "runtime_capability_manifest_digest",
                ):
                    if column not in columns:
                        connection.execute(f"ALTER TABLE releases ADD COLUMN {column} TEXT")

        await asyncio.to_thread(operation)

    async def healthcheck(self) -> bool:
        """处理 healthcheck 对应的当前组件内部业务步骤。


        Perform healthcheck within the SqliteRepository ownership boundary.
        """

        def operation() -> bool:
            """在独立数据库线程中执行本次受控操作，连接与事务边界由外层读写助手统一管理。"""
            with self._connect() as connection:
                row = connection.execute("SELECT 1 AS ok").fetchone()
                return bool(row and row["ok"] == 1)

        return await asyncio.to_thread(operation)

    async def acquire_lease(self, lease_name: str, owner_id: str, ttl_seconds: float) -> bool:
        """处理 acquire_lease 对应的当前组件内部业务步骤。


        Acquire or renew only an expired/self-owned lease using one write transaction.
        """
        now = datetime.now(UTC)
        expires_at = now + timedelta(seconds=ttl_seconds)

        def operation(connection: sqlite3.Connection) -> bool:
            """在独立数据库线程中执行本次受控操作，连接与事务边界由外层读写助手统一管理。"""
            cursor = connection.execute(
                """
                INSERT INTO controller_leases(lease_name, owner_id, expires_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(lease_name) DO UPDATE SET
                    owner_id = excluded.owner_id,
                    expires_at = excluded.expires_at,
                    updated_at = excluded.updated_at
                WHERE controller_leases.owner_id = excluded.owner_id
                   OR controller_leases.expires_at <= excluded.updated_at
                """,
                (
                    lease_name,
                    owner_id,
                    expires_at.isoformat(),
                    now.isoformat(),
                ),
            )
            return cursor.rowcount == 1

        return await self._write(operation)

    async def create_agent(self, agent: AgentDefinition, event: OutboxEvent) -> None:
        """创建或构建 create_agent 对应的受控业务步骤。


        Persist state while preserving the transaction and audit boundary.
        """

        def operation(connection: sqlite3.Connection) -> None:
            """在独立数据库线程中执行本次受控操作，连接与事务边界由外层读写助手统一管理。"""
            connection.execute(
                """
                INSERT INTO agents (
                    tenant_id, agent_id, revision, draft_json, created_by, updated_by,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    agent.tenant_id,
                    agent.agent_id,
                    agent.revision,
                    _json(agent.draft.model_dump(mode="json")),
                    agent.created_by,
                    agent.updated_by,
                    agent.created_at.isoformat(),
                    agent.updated_at.isoformat(),
                ),
            )
            self._insert_event(connection, event)

        await self._write(operation)

    async def update_agent(
        self,
        agent: AgentDefinition,
        expected_revision: int,
        event: OutboxEvent,
    ) -> bool:
        """更新 update_agent 对应的受控业务步骤。


        Apply the requested state transition with configured consistency checks.
        """

        def operation(connection: sqlite3.Connection) -> bool:
            """在独立数据库线程中执行本次受控操作，连接与事务边界由外层读写助手统一管理。"""
            cursor = connection.execute(
                """
                UPDATE agents
                SET revision = ?, draft_json = ?, updated_by = ?, updated_at = ?
                WHERE tenant_id = ? AND agent_id = ? AND revision = ?
                """,
                (
                    agent.revision,
                    _json(agent.draft.model_dump(mode="json")),
                    agent.updated_by,
                    agent.updated_at.isoformat(),
                    agent.tenant_id,
                    agent.agent_id,
                    expected_revision,
                ),
            )
            if cursor.rowcount == 1:
                self._insert_event(connection, event)
                return True
            return False

        return await self._write(operation)

    async def get_agent(self, tenant_id: str, agent_id: str) -> AgentDefinition | None:
        """读取或查询 get_agent 对应的受控业务步骤。


        Return the requested value through the established ownership boundary.
        """

        def operation(connection: sqlite3.Connection) -> AgentDefinition | None:
            """在独立数据库线程中执行本次受控操作，连接与事务边界由外层读写助手统一管理。"""
            row = connection.execute(
                "SELECT * FROM agents WHERE tenant_id = ? AND agent_id = ?",
                (tenant_id, agent_id),
            ).fetchone()
            return _agent_from_row(row) if row else None

        return await self._read(operation)

    async def list_agents(self, tenant_id: str) -> list[AgentDefinition]:
        """读取或查询 list_agents 对应的受控业务步骤。


        List only values visible within the caller's tenant and lifecycle scope.
        """

        def operation(connection: sqlite3.Connection) -> list[AgentDefinition]:
            """在独立数据库线程中执行本次受控操作，连接与事务边界由外层读写助手统一管理。"""
            rows = connection.execute(
                "SELECT * FROM agents WHERE tenant_id = ? ORDER BY updated_at DESC",
                (tenant_id,),
            ).fetchall()
            return [_agent_from_row(row) for row in rows]

        return await self._read(operation)

    async def create_version(self, version: AgentVersion, event: OutboxEvent) -> None:
        """创建或构建 create_version 对应的受控业务步骤。


        Persist state while preserving the transaction and audit boundary.
        """

        def operation(connection: sqlite3.Connection) -> None:
            """在独立数据库线程中执行本次受控操作，连接与事务边界由外层读写助手统一管理。"""
            connection.execute(
                """
                INSERT INTO agent_versions (
                    tenant_id, version_id, agent_id, semantic_version, source_revision,
                    content_hash, snapshot_json, change_summary, published_by, published_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    version.tenant_id,
                    version.version_id,
                    version.agent_id,
                    version.semantic_version,
                    version.source_revision,
                    version.content_hash,
                    _json(version.snapshot.model_dump(mode="json")),
                    version.change_summary,
                    version.published_by,
                    version.published_at.isoformat(),
                ),
            )
            self._insert_event(connection, event)

        await self._write(operation)

    async def create_skill(self, skill: SkillDefinition, event: OutboxEvent) -> None:
        """在与 Outbox 相同的事务中创建租户级 Skill 草稿。"""

        def operation(connection: sqlite3.Connection) -> None:
            """写入草稿后记录不可变创建事实，失败时两者一并回滚。"""
            connection.execute(
                """INSERT INTO skills (
                   tenant_id, skill_id, revision, draft_json,
                   created_by, updated_by, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    skill.tenant_id,
                    skill.skill_id,
                    skill.revision,
                    _json(skill.draft.model_dump(mode="json")),
                    skill.created_by,
                    skill.updated_by,
                    skill.created_at.isoformat(),
                    skill.updated_at.isoformat(),
                ),
            )
            self._insert_event(connection, event)

        await self._write(operation)

    async def get_skill(self, tenant_id: str, skill_id: str) -> SkillDefinition | None:
        """按租户和 Skill ID 读取草稿，避免跨租户能力目录泄露。"""

        def operation(connection: sqlite3.Connection) -> SkillDefinition | None:
            """在只读连接中把持久化行转换为严格领域模型。"""
            row = connection.execute(
                "SELECT * FROM skills WHERE tenant_id = ? AND skill_id = ?", (tenant_id, skill_id)
            ).fetchone()
            return _skill_from_row(row) if row else None

        return await self._read(operation)

    async def update_skill(
        self, skill: SkillDefinition, expected_revision: int, event: OutboxEvent
    ) -> bool:
        """以 CAS 更新 Skill 草稿，防止并发编辑静默覆盖彼此配置。"""

        def operation(connection: sqlite3.Connection) -> bool:
            """仅在修订号匹配时更新草稿并写入同事务审计事件。"""
            cursor = connection.execute(
                """UPDATE skills SET revision = ?, draft_json = ?, updated_by = ?, updated_at = ?
                   WHERE tenant_id = ? AND skill_id = ? AND revision = ?""",
                (
                    skill.revision,
                    _json(skill.draft.model_dump(mode="json")),
                    skill.updated_by,
                    skill.updated_at.isoformat(),
                    skill.tenant_id,
                    skill.skill_id,
                    expected_revision,
                ),
            )
            if cursor.rowcount != 1:
                return False
            self._insert_event(connection, event)
            return True

        return await self._write(operation)

    async def create_skill_version(self, version: SkillVersion, event: OutboxEvent) -> None:
        """持久化不可变 Skill 工件与 Outbox 事实，禁止只写其一。"""

        def operation(connection: sqlite3.Connection) -> None:
            """在单事务内插入版本及其可审计发布事件。"""
            connection.execute(
                """INSERT INTO skill_versions (
                   tenant_id, version_id, skill_id, semantic_version,
                   source_revision, artifact_digest,
                   plan_json, status, change_summary, published_by, published_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    version.tenant_id,
                    version.version_id,
                    version.skill_id,
                    version.semantic_version,
                    version.source_revision,
                    version.artifact_digest,
                    _json(version.plan.model_dump(mode="json")),
                    version.status.value,
                    version.change_summary,
                    version.published_by,
                    version.published_at.isoformat(),
                    version.updated_at.isoformat(),
                ),
            )
            self._insert_event(connection, event)

        await self._write(operation)

    async def get_skill_version_by_semantic(
        self, tenant_id: str, skill_id: str, semantic_version: str
    ) -> SkillVersion | None:
        """按 Agent 快照中的语义版本读取 Skill，供发布前摘要和资格校验。"""

        def operation(connection: sqlite3.Connection) -> SkillVersion | None:
            """读取唯一语义版本，不在运行期按“最新”解析。"""
            row = connection.execute(
                """SELECT * FROM skill_versions
                   WHERE tenant_id = ? AND skill_id = ? AND semantic_version = ?""",
                (tenant_id, skill_id, semantic_version),
            ).fetchone()
            return _skill_version_from_row(row) if row else None

        return await self._read(operation)

    async def get_skill_version(
        self, tenant_id: str, skill_id: str, version_id: str
    ) -> SkillVersion | None:
        """按内部版本 ID 读取 Skill 工件，供治理状态转换使用。"""

        def operation(connection: sqlite3.Connection) -> SkillVersion | None:
            """执行租户和 Skill 双重过滤，避免全局版本 ID 越权读取。"""
            row = connection.execute(
                """SELECT * FROM skill_versions
                   WHERE tenant_id = ? AND skill_id = ? AND version_id = ?""",
                (tenant_id, skill_id, version_id),
            ).fetchone()
            return _skill_version_from_row(row) if row else None

        return await self._read(operation)

    async def list_active_skill_versions(self, tenant_id: str) -> list[SkillVersion]:
        """只列出当前租户 Active 工件，供渐进式目录生成 Skill Card。"""

        def operation(connection: sqlite3.Connection) -> list[SkillVersion]:
            """按 Skill/语义版本稳定排序，不暴露草稿和非准入工件。"""
            rows = connection.execute(
                """SELECT * FROM skill_versions
                   WHERE tenant_id = ? AND status = ?
                   ORDER BY skill_id, semantic_version""",
                (tenant_id, SkillStatus.ACTIVE.value),
            ).fetchall()
            return [_skill_version_from_row(row) for row in rows]

        return await self._read(operation)

    async def update_skill_status(self, version: SkillVersion, event: OutboxEvent) -> None:
        """只变更工件可用状态；计划和摘要保持不可变。"""

        def operation(connection: sqlite3.Connection) -> None:
            """提交状态变化和审计事实，供 Runtime 健康降级和追溯。"""
            connection.execute(
                """UPDATE skill_versions SET status = ?, updated_at = ?
                   WHERE tenant_id = ? AND skill_id = ? AND version_id = ?""",
                (
                    version.status.value,
                    version.updated_at.isoformat(),
                    version.tenant_id,
                    version.skill_id,
                    version.version_id,
                ),
            )
            self._insert_event(connection, event)

        await self._write(operation)

    async def create_workflow(self, item: WorkflowDefinition, event: OutboxEvent) -> None:
        """原子创建 Workflow Draft 与审计事件。"""

        def operation(connection: sqlite3.Connection) -> None:
            """将 Draft 与 Outbox 写在同一个事务中。"""
            connection.execute(
                """INSERT INTO workflows (
                   tenant_id, workflow_id, revision, draft_json,
                   created_by, updated_by, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    item.tenant_id,
                    item.workflow_id,
                    item.revision,
                    _json(item.draft.model_dump(mode="json")),
                    item.created_by,
                    item.updated_by,
                    item.created_at.isoformat(),
                    item.updated_at.isoformat(),
                ),
            )
            self._insert_event(connection, event)

        await self._write(operation)

    async def get_workflow(self, tenant_id: str, workflow_id: str) -> WorkflowDefinition | None:
        """按租户读取 Workflow Draft。"""

        def operation(connection: sqlite3.Connection) -> WorkflowDefinition | None:
            """读取并还原严格 WorkflowDefinition。"""
            row = connection.execute(
                "SELECT * FROM workflows WHERE tenant_id = ? AND workflow_id = ?",
                (tenant_id, workflow_id),
            ).fetchone()
            return _workflow_from_row(row) if row else None

        return await self._read(operation)

    async def update_workflow(
        self, item: WorkflowDefinition, expected_revision: int, event: OutboxEvent
    ) -> bool:
        """通过修订号 CAS 更新 Workflow Draft。"""

        def operation(connection: sqlite3.Connection) -> bool:
            """仅在预期修订号仍有效时提交变更和事件。"""
            cursor = connection.execute(
                """UPDATE workflows SET revision = ?, draft_json = ?, updated_by = ?, updated_at = ?
                   WHERE tenant_id = ? AND workflow_id = ? AND revision = ?""",
                (
                    item.revision,
                    _json(item.draft.model_dump(mode="json")),
                    item.updated_by,
                    item.updated_at.isoformat(),
                    item.tenant_id,
                    item.workflow_id,
                    expected_revision,
                ),
            )
            if cursor.rowcount != 1:
                return False
            self._insert_event(connection, event)
            return True

        return await self._write(operation)

    async def create_workflow_version(self, item: WorkflowVersion, event: OutboxEvent) -> None:
        """原子保存不可变 WorkflowVersion 和发布事件。"""

        def operation(connection: sqlite3.Connection) -> None:
            """写入已编译计划，禁止后续覆盖。"""
            connection.execute(
                """INSERT INTO workflow_versions (
                   tenant_id, version_id, workflow_id, semantic_version, source_revision,
                   artifact_digest, plan_json, published_by, published_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    item.tenant_id,
                    item.version_id,
                    item.workflow_id,
                    item.semantic_version,
                    item.source_revision,
                    item.artifact_digest,
                    _json(item.plan.model_dump(mode="json")),
                    item.published_by,
                    item.published_at.isoformat(),
                ),
            )
            self._insert_event(connection, event)

        await self._write(operation)

    async def get_workflow_version(
        self, tenant_id: str, workflow_id: str, version_id: str
    ) -> WorkflowVersion | None:
        """按租户、Workflow 和版本三元范围读取冻结工件。"""

        def operation(connection: sqlite3.Connection) -> WorkflowVersion | None:
            """拒绝仅凭全局版本 ID 跨租户读取。"""
            row = connection.execute(
                """SELECT * FROM workflow_versions
                   WHERE tenant_id = ? AND workflow_id = ? AND version_id = ?""",
                (tenant_id, workflow_id, version_id),
            ).fetchone()
            return _workflow_version_from_row(row) if row else None

        return await self._read(operation)

    async def get_workflow_version_by_semantic(
        self, tenant_id: str, workflow_id: str, semantic_version: str
    ) -> WorkflowVersion | None:
        """按精确语义版本解析 Workflow Provider，不使用 latest。"""

        def operation(connection: sqlite3.Connection) -> WorkflowVersion | None:
            """在租户和 Workflow 双重边界内读取不可变工件。"""
            row = connection.execute(
                """SELECT * FROM workflow_versions
                   WHERE tenant_id = ? AND workflow_id = ? AND semantic_version = ?""",
                (tenant_id, workflow_id, semantic_version),
            ).fetchone()
            return _workflow_version_from_row(row) if row else None

        return await self._read(operation)

    async def create_workflow_release(self, item: WorkflowRelease, event: OutboxEvent) -> None:
        """激活新 Workflow Release，并在同事务退役原 Active 版本。"""

        def operation(connection: sqlite3.Connection) -> None:
            """以单事务保证同环境解析不会观察到两个 Active 版本。"""
            connection.execute(
                """UPDATE workflow_releases SET status = 'retired'
                   WHERE tenant_id = ? AND workflow_id = ? AND environment = ?
                     AND status = 'active'""",
                (item.tenant_id, item.workflow_id, item.environment),
            )
            connection.execute(
                """INSERT INTO workflow_releases (
                   tenant_id, release_id, workflow_id, version_id, environment,
                   status, created_by, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    item.tenant_id,
                    item.release_id,
                    item.workflow_id,
                    item.version_id,
                    item.environment,
                    item.status,
                    item.created_by,
                    item.created_at.isoformat(),
                ),
            )
            self._insert_event(connection, event)

        await self._write(operation)

    async def resolve_workflow_release(
        self, tenant_id: str, workflow_id: str, environment: str
    ) -> tuple[WorkflowRelease, WorkflowVersion] | None:
        """解析当前 Active Release 及其不可变 WorkflowVersion。"""

        def operation(connection: sqlite3.Connection):
            """在同一只读快照中读取 Release 与 Version，避免版本错配。"""
            row = connection.execute(
                """SELECT r.*, v.semantic_version, v.source_revision, v.artifact_digest,
                          v.plan_json, v.published_by, v.published_at
                   FROM workflow_releases r JOIN workflow_versions v ON v.version_id = r.version_id
                   WHERE r.tenant_id = ? AND r.workflow_id = ? AND r.environment = ?
                     AND r.status = 'active' ORDER BY r.created_at DESC LIMIT 1""",
                (tenant_id, workflow_id, environment),
            ).fetchone()
            if not row:
                return None
            release = _workflow_release_from_row(row)
            version = _workflow_version_from_row(row)
            return release, version

        return await self._read(operation)

    async def get_version(
        self,
        tenant_id: str,
        agent_id: str,
        version_id: str,
    ) -> AgentVersion | None:
        """读取或查询 get_version 对应的受控业务步骤。


        Return the requested value through the established ownership boundary.
        """

        def operation(connection: sqlite3.Connection) -> AgentVersion | None:
            """在独立数据库线程中执行本次受控操作，连接与事务边界由外层读写助手统一管理。"""
            row = connection.execute(
                """
                SELECT * FROM agent_versions
                WHERE tenant_id = ? AND agent_id = ? AND version_id = ?
                """,
                (tenant_id, agent_id, version_id),
            ).fetchone()
            return _version_from_row(row) if row else None

        return await self._read(operation)

    async def list_versions(self, tenant_id: str, agent_id: str) -> list[AgentVersion]:
        """读取或查询 list_versions 对应的受控业务步骤。


        List only values visible within the caller's tenant and lifecycle scope.
        """

        def operation(connection: sqlite3.Connection) -> list[AgentVersion]:
            """在独立数据库线程中执行本次受控操作，连接与事务边界由外层读写助手统一管理。"""
            rows = connection.execute(
                """
                SELECT * FROM agent_versions
                WHERE tenant_id = ? AND agent_id = ?
                ORDER BY published_at DESC
                """,
                (tenant_id, agent_id),
            ).fetchall()
            return [_version_from_row(row) for row in rows]

        return await self._read(operation)

    async def create_release(
        self,
        release: ReleaseManifest,
        event: OutboxEvent,
        retire_release_id: str | None = None,
    ) -> None:
        """创建或构建 create_release 对应的受控业务步骤。


        Persist state while preserving the transaction and audit boundary.
        """

        def operation(connection: sqlite3.Connection) -> None:
            """在独立数据库线程中执行本次受控操作，连接与事务边界由外层读写助手统一管理。"""
            connection.execute(
                """
                INSERT INTO releases (
                    tenant_id, release_id, agent_id, version_id, environment,
                    rollout_percentage, tenant_allowlist_json, status, previous_release_id,
                    reason, quality_gate_id, quality_gate_metrics_json, agent_lab_experiment_id,
                    runtime_executor_catalog_version, runtime_executor_cluster_id,
                    runtime_executor_catalog_hash, runtime_capability_manifest_digest,
                    created_by, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    release.tenant_id,
                    release.release_id,
                    release.agent_id,
                    release.version_id,
                    release.environment,
                    release.rollout_percentage,
                    _json(release.tenant_allowlist),
                    release.status.value,
                    release.previous_release_id,
                    release.reason,
                    release.quality_gate_id,
                    _json(release.quality_gate_metrics),
                    release.agent_lab_experiment_id,
                    release.runtime_executor_catalog_version,
                    release.runtime_executor_cluster_id,
                    release.runtime_executor_catalog_hash,
                    release.runtime_capability_manifest_digest,
                    release.created_by,
                    release.created_at.isoformat(),
                    release.updated_at.isoformat(),
                ),
            )
            if retire_release_id:
                connection.execute(
                    """
                    UPDATE releases SET status = ?, updated_at = ?
                    WHERE tenant_id = ? AND release_id = ?
                    """,
                    (
                        ReleaseStatus.RETIRED.value,
                        release.updated_at.isoformat(),
                        release.tenant_id,
                        retire_release_id,
                    ),
                )
            self._insert_event(connection, event)

        await self._write(operation)

    async def update_release(
        self,
        release: ReleaseManifest,
        event: OutboxEvent,
        related_release_id: str | None = None,
        related_status: ReleaseStatus | None = None,
        expected_updated_at: str | None = None,
    ) -> bool:
        """更新 update_release 对应的受控业务步骤。


        Apply the requested state transition with configured consistency checks.
        """

        def operation(connection: sqlite3.Connection) -> bool:
            """在独立数据库线程中执行本次受控操作，连接与事务边界由外层读写助手统一管理。"""
            cursor = connection.execute(
                """
                UPDATE releases
                SET rollout_percentage = ?, tenant_allowlist_json = ?, status = ?, updated_at = ?
                WHERE tenant_id = ? AND release_id = ?
                  AND (? IS NULL OR updated_at = ?)
                """,
                (
                    release.rollout_percentage,
                    _json(release.tenant_allowlist),
                    release.status.value,
                    release.updated_at.isoformat(),
                    release.tenant_id,
                    release.release_id,
                    expected_updated_at,
                    expected_updated_at,
                ),
            )
            if cursor.rowcount != 1:
                return False
            if related_release_id and related_status:
                connection.execute(
                    """
                    UPDATE releases SET status = ?, updated_at = ?
                    WHERE tenant_id = ? AND release_id = ?
                    """,
                    (
                        related_status.value,
                        release.updated_at.isoformat(),
                        release.tenant_id,
                        related_release_id,
                    ),
                )
            self._insert_event(connection, event)
            return True

        return await self._write(operation)

    async def get_release(self, tenant_id: str, release_id: str) -> ReleaseManifest | None:
        """读取或查询 get_release 对应的受控业务步骤。


        Return the requested value through the established ownership boundary.
        """

        def operation(connection: sqlite3.Connection) -> ReleaseManifest | None:
            """在独立数据库线程中执行本次受控操作，连接与事务边界由外层读写助手统一管理。"""
            row = connection.execute(
                "SELECT * FROM releases WHERE tenant_id = ? AND release_id = ?",
                (tenant_id, release_id),
            ).fetchone()
            return _release_from_row(row) if row else None

        return await self._read(operation)

    async def list_releases(
        self,
        tenant_id: str,
        agent_id: str,
        environment: str | None = None,
    ) -> list[ReleaseManifest]:
        """读取或查询 list_releases 对应的受控业务步骤。


        List only values visible within the caller's tenant and lifecycle scope.
        """

        def operation(connection: sqlite3.Connection) -> list[ReleaseManifest]:
            """在独立数据库线程中执行本次受控操作，连接与事务边界由外层读写助手统一管理。"""
            if environment:
                rows = connection.execute(
                    """
                    SELECT * FROM releases
                    WHERE tenant_id = ? AND agent_id = ? AND environment = ?
                    ORDER BY created_at DESC
                    """,
                    (tenant_id, agent_id, environment),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT * FROM releases
                    WHERE tenant_id = ? AND agent_id = ?
                    ORDER BY created_at DESC
                    """,
                    (tenant_id, agent_id),
                ).fetchall()
            return [_release_from_row(row) for row in rows]

        return await self._read(operation)

    async def get_session_binding(
        self,
        tenant_id: str,
        agent_id: str,
        environment: str,
        session_id: str,
    ) -> dict[str, str] | None:
        """读取或查询 get_session_binding 对应的受控业务步骤。


        Return the requested value through the established ownership boundary.
        """

        def operation(connection: sqlite3.Connection) -> dict[str, str] | None:
            """在独立数据库线程中执行本次受控操作，连接与事务边界由外层读写助手统一管理。"""
            row = connection.execute(
                """
                SELECT release_id, assignment FROM session_bindings
                WHERE tenant_id = ? AND agent_id = ? AND environment = ? AND session_id = ?
                """,
                (tenant_id, agent_id, environment, session_id),
            ).fetchone()
            return dict(row) if row else None

        return await self._read(operation)

    async def bind_session(
        self,
        tenant_id: str,
        agent_id: str,
        environment: str,
        session_id: str,
        release_id: str,
        assignment: str,
        timestamp: str,
    ) -> None:
        """处理 bind_session 对应的当前组件内部业务步骤。


        Persist state while preserving the transaction and audit boundary.
        """

        def operation(connection: sqlite3.Connection) -> None:
            """在独立数据库线程中执行本次受控操作，连接与事务边界由外层读写助手统一管理。"""
            connection.execute(
                """
                INSERT INTO session_bindings (
                    tenant_id, agent_id, environment, session_id, release_id,
                    assignment, bound_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (tenant_id, agent_id, environment, session_id)
                DO UPDATE SET release_id = excluded.release_id,
                              assignment = excluded.assignment,
                              updated_at = excluded.updated_at
                """,
                (
                    tenant_id,
                    agent_id,
                    environment,
                    session_id,
                    release_id,
                    assignment,
                    timestamp,
                    timestamp,
                ),
            )

        await self._write(operation)

    async def get_tenant_policy(self, tenant_id: str) -> TenantPolicy | None:
        """读取或查询 get_tenant_policy 对应的受控业务步骤。


        Return the requested value through the established ownership boundary.
        """

        def operation(connection: sqlite3.Connection) -> TenantPolicy | None:
            """在独立数据库线程中执行本次受控操作，连接与事务边界由外层读写助手统一管理。"""
            row = connection.execute(
                "SELECT policy_json FROM tenant_policies WHERE tenant_id = ?",
                (tenant_id,),
            ).fetchone()
            return TenantPolicy.model_validate_json(row["policy_json"]) if row else None

        return await self._read(operation)

    async def upsert_tenant_policy(self, policy: TenantPolicy, event: OutboxEvent) -> None:
        """处理 upsert_tenant_policy 对应的当前组件内部业务步骤。


        Persist state while preserving the transaction and audit boundary.
        """

        def operation(connection: sqlite3.Connection) -> None:
            """在独立数据库线程中执行本次受控操作，连接与事务边界由外层读写助手统一管理。"""
            connection.execute(
                """
                INSERT INTO tenant_policies (tenant_id, policy_json, updated_by, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT (tenant_id)
                DO UPDATE SET policy_json = excluded.policy_json,
                              updated_by = excluded.updated_by,
                              updated_at = excluded.updated_at
                """,
                (
                    policy.tenant_id,
                    policy.model_dump_json(),
                    policy.updated_by,
                    policy.updated_at.isoformat(),
                ),
            )
            self._insert_event(connection, event)

        await self._write(operation)

    async def list_outbox(
        self,
        tenant_id: str,
        after_sequence: int,
        limit: int,
    ) -> tuple[list[OutboxEvent], int | None]:
        """读取或查询 list_outbox 对应的受控业务步骤。


        List only values visible within the caller's tenant and lifecycle scope.
        """

        def operation(connection: sqlite3.Connection) -> tuple[list[OutboxEvent], int | None]:
            """在独立数据库线程中执行本次受控操作，连接与事务边界由外层读写助手统一管理。"""
            rows = connection.execute(
                """
                SELECT * FROM outbox_events
                WHERE tenant_id = ? AND sequence > ?
                ORDER BY sequence ASC
                LIMIT ?
                """,
                (tenant_id, after_sequence, limit),
            ).fetchall()
            items = [_event_from_row(row) for row in rows]
            next_cursor = int(rows[-1]["sequence"]) if rows else None
            return items, next_cursor

        return await self._read(operation)

    async def save_model_release(
        self, tenant_id: str, release_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """持久化 save_model_release 对应的受控业务步骤。


        Persist state while preserving the transaction and audit boundary.
        """
        created = str(payload["startedAt"])
        updated = str(payload["updatedAt"])

        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            """在独立数据库线程中执行本次受控操作，连接与事务边界由外层读写助手统一管理。"""
            connection.execute(
                """
                INSERT INTO model_route_releases (
                    tenant_id, release_id, payload_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (tenant_id, release_id) DO UPDATE SET
                    payload_json = excluded.payload_json,
                    updated_at = excluded.updated_at
                """,
                (tenant_id, release_id, _json(payload), created, updated),
            )
            return payload

        return await self._write(operation)

    async def get_model_release(self, tenant_id: str, release_id: str) -> dict[str, Any] | None:
        """读取或查询 get_model_release 对应的受控业务步骤。


        Return the requested value through the established ownership boundary.
        """

        def operation(connection: sqlite3.Connection) -> dict[str, Any] | None:
            """在独立数据库线程中执行本次受控操作，连接与事务边界由外层读写助手统一管理。"""
            row = connection.execute(
                """
                SELECT payload_json FROM model_route_releases
                WHERE tenant_id = ? AND release_id = ?
                """,
                (tenant_id, release_id),
            ).fetchone()
            return json.loads(row["payload_json"]) if row else None

        return await self._read(operation)

    async def list_model_releases(self, tenant_id: str) -> list[dict[str, Any]]:
        """读取或查询 list_model_releases 对应的受控业务步骤。


        List only values visible within the caller's tenant and lifecycle scope.
        """

        def operation(connection: sqlite3.Connection) -> list[dict[str, Any]]:
            """在独立数据库线程中执行本次受控操作，连接与事务边界由外层读写助手统一管理。"""
            rows = connection.execute(
                """
                SELECT payload_json FROM model_route_releases
                WHERE tenant_id = ? ORDER BY updated_at DESC
                """,
                (tenant_id,),
            ).fetchall()
            return [json.loads(row["payload_json"]) for row in rows]

        return await self._read(operation)

    async def list_active_model_releases(self) -> list[tuple[str, dict[str, Any]]]:
        """读取或查询 list_active_model_releases 对应的受控业务步骤。


        List only values visible within the caller's tenant and lifecycle scope.
        """

        def operation(
            connection: sqlite3.Connection,
        ) -> list[tuple[str, dict[str, Any]]]:
            """在独立数据库线程中执行本次受控操作，连接与事务边界由外层读写助手统一管理。"""
            rows = connection.execute(
                """
                SELECT tenant_id, payload_json FROM model_route_releases
                ORDER BY updated_at ASC
                """
            ).fetchall()
            result = []
            for row in rows:
                payload = json.loads(row["payload_json"])
                if payload.get("status") in {"CANARY_ACTIVE", "MONITORING"}:
                    result.append((str(row["tenant_id"]), payload))
            return result

        return await self._read(operation)

    async def _read(self, operation: Callable[[sqlite3.Connection], T]) -> T:
        """处理 _read 对应的当前组件内部业务步骤。


        Internal helper for SqliteRepository; preserve its caller-facing invariant.
        """

        def run() -> T:
            """处理 run 对应的当前组件内部业务步骤。


            Perform run within the module ownership boundary.
            """
            with self._connect() as connection:
                return operation(connection)

        return await asyncio.to_thread(run)

    async def _write(self, operation: Callable[[sqlite3.Connection], T]) -> T:
        """处理 _write 对应的当前组件内部业务步骤。


        Internal helper for SqliteRepository; preserve its caller-facing invariant.
        """
        async with self._write_lock:

            def run() -> T:
                """处理 run 对应的当前组件内部业务步骤。


                Perform run within the module ownership boundary.
                """
                with self._connect() as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    try:
                        result = operation(connection)
                        connection.commit()
                        return result
                    except Exception:
                        connection.rollback()
                        raise

            return await asyncio.to_thread(run)

    def _connect(self) -> sqlite3.Connection:
        """处理 _connect 对应的当前组件内部业务步骤。


        Internal helper for SqliteRepository; preserve its caller-facing invariant.
        """
        connection = sqlite3.connect(self._database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @staticmethod
    def _insert_event(connection: sqlite3.Connection, event: OutboxEvent) -> None:
        """处理 _insert_event 对应的当前组件内部业务步骤。


        Internal helper for SqliteRepository; preserve its caller-facing invariant.
        """
        connection.execute(
            """
            INSERT INTO outbox_events (
                event_id, event_type, trace_id, tenant_id, aggregate_type,
                aggregate_id, schema_version, occurred_at, payload_json, published_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.event_id,
                event.event_type,
                event.trace_id,
                event.tenant_id,
                event.aggregate_type,
                event.aggregate_id,
                event.schema_version,
                event.occurred_at.isoformat(),
                _json(event.payload),
                event.published_at.isoformat() if event.published_at else None,
            ),
        )


def _json(value: Any) -> str:
    """处理 _json 对应的当前组件内部业务步骤。


    Internal helper for module; preserve its caller-facing invariant.
    """
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


class SqliteRepository(ControlPlaneRepositoryOperations):
    """本地 SQLite 事务实现；生产 PostgreSQL 不继承此具体后端类。"""


def _agent_from_row(row: sqlite3.Row) -> AgentDefinition:
    """处理 _agent_from_row 对应的当前组件内部业务步骤。


    Internal helper for module; preserve its caller-facing invariant.
    """
    return AgentDefinition.model_validate(
        {
            "tenant_id": row["tenant_id"],
            "agent_id": row["agent_id"],
            "revision": row["revision"],
            "draft": json.loads(row["draft_json"]),
            "created_by": row["created_by"],
            "updated_by": row["updated_by"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
    )


def _version_from_row(row: sqlite3.Row) -> AgentVersion:
    """处理 _version_from_row 对应的当前组件内部业务步骤。


    Internal helper for module; preserve its caller-facing invariant.
    """
    return AgentVersion.model_validate(
        {
            "tenant_id": row["tenant_id"],
            "version_id": row["version_id"],
            "agent_id": row["agent_id"],
            "semantic_version": row["semantic_version"],
            "source_revision": row["source_revision"],
            "content_hash": row["content_hash"],
            "snapshot": json.loads(row["snapshot_json"]),
            "change_summary": row["change_summary"],
            "published_by": row["published_by"],
            "published_at": row["published_at"],
        }
    )


def _skill_from_row(row: sqlite3.Row) -> SkillDefinition:
    """将持久化的 Skill 草稿行还原为严格领域模型。"""
    return SkillDefinition.model_validate(
        {
            "tenant_id": row["tenant_id"],
            "skill_id": row["skill_id"],
            "revision": row["revision"],
            "draft": json.loads(row["draft_json"]),
            "created_by": row["created_by"],
            "updated_by": row["updated_by"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
    )


def _skill_version_from_row(row: sqlite3.Row) -> SkillVersion:
    """将冻结 SkillVersion 行还原为运行时可校验的编译计划。"""
    return SkillVersion.model_validate(
        {
            "tenant_id": row["tenant_id"],
            "version_id": row["version_id"],
            "skill_id": row["skill_id"],
            "semantic_version": row["semantic_version"],
            "source_revision": row["source_revision"],
            "artifact_digest": row["artifact_digest"],
            "plan": json.loads(row["plan_json"]),
            "status": row["status"],
            "change_summary": row["change_summary"],
            "published_by": row["published_by"],
            "published_at": row["published_at"],
            "updated_at": row["updated_at"],
        }
    )


def _workflow_from_row(row: sqlite3.Row) -> WorkflowDefinition:
    """将 Workflow Draft 持久化行还原为严格领域模型。"""
    return WorkflowDefinition.model_validate(
        {
            "tenant_id": row["tenant_id"],
            "workflow_id": row["workflow_id"],
            "revision": row["revision"],
            "draft": json.loads(row["draft_json"]),
            "created_by": row["created_by"],
            "updated_by": row["updated_by"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
    )


def _workflow_version_from_row(row: sqlite3.Row) -> WorkflowVersion:
    """将冻结 WorkflowVersion 行还原为编译计划。"""
    return WorkflowVersion.model_validate(
        {
            "tenant_id": row["tenant_id"],
            "version_id": row["version_id"],
            "workflow_id": row["workflow_id"],
            "semantic_version": row["semantic_version"],
            "source_revision": row["source_revision"],
            "artifact_digest": row["artifact_digest"],
            "plan": json.loads(row["plan_json"]),
            "published_by": row["published_by"],
            "published_at": row["published_at"],
        }
    )


def _workflow_release_from_row(row: sqlite3.Row) -> WorkflowRelease:
    """将 Active/Retired Workflow Release 行还原为领域模型。"""
    return WorkflowRelease.model_validate(
        {
            "tenant_id": row["tenant_id"],
            "release_id": row["release_id"],
            "workflow_id": row["workflow_id"],
            "version_id": row["version_id"],
            "environment": row["environment"],
            "status": row["status"],
            "created_by": row["created_by"],
            "created_at": row["created_at"],
        }
    )


def _release_from_row(row: sqlite3.Row) -> ReleaseManifest:
    """处理 _release_from_row 对应的当前组件内部业务步骤。


    Internal helper for module; preserve its caller-facing invariant.
    """
    return ReleaseManifest.model_validate(
        {
            "tenant_id": row["tenant_id"],
            "release_id": row["release_id"],
            "agent_id": row["agent_id"],
            "version_id": row["version_id"],
            "environment": row["environment"],
            "rollout_percentage": row["rollout_percentage"],
            "tenant_allowlist": json.loads(row["tenant_allowlist_json"]),
            "status": row["status"],
            "previous_release_id": row["previous_release_id"],
            "reason": row["reason"],
            "quality_gate_id": row["quality_gate_id"],
            "quality_gate_metrics": json.loads(row["quality_gate_metrics_json"]),
            "agent_lab_experiment_id": row["agent_lab_experiment_id"],
            "runtime_executor_catalog_version": row["runtime_executor_catalog_version"],
            "runtime_executor_cluster_id": row["runtime_executor_cluster_id"],
            "runtime_executor_catalog_hash": row["runtime_executor_catalog_hash"],
            "runtime_capability_manifest_digest": row["runtime_capability_manifest_digest"],
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
    )


def _event_from_row(row: sqlite3.Row) -> OutboxEvent:
    """处理 _event_from_row 对应的当前组件内部业务步骤。


    Internal helper for module; preserve its caller-facing invariant.
    """
    return OutboxEvent.model_validate(
        {
            "event_id": row["event_id"],
            "event_type": row["event_type"],
            "trace_id": row["trace_id"],
            "tenant_id": row["tenant_id"],
            "aggregate_type": row["aggregate_type"],
            "aggregate_id": row["aggregate_id"],
            "schema_version": row["schema_version"],
            "occurred_at": row["occurred_at"],
            "payload": json.loads(row["payload_json"]),
            "published_at": row["published_at"],
        }
    )
