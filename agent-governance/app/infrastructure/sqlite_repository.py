"""Local transactional audit ledger reference implementation.

Production PostgreSQL uses the same append-only ownership and hash-chain
semantics; this adapter exists for deterministic development and tests.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

from app.domain.models import AuditEvent, Finding, FindingStatus, GovernanceEvent, TenantPolicy

T = TypeVar("T")


class GovernanceRepositoryOperations:
    """与数据库方言无关的治理聚合操作；具体连接、锁与迁移由适配器负责。"""

    def __init__(self, database_path: Path, schema_path: Path) -> None:
        """保存数据库与建表脚本路径，并初始化串行写锁以维护审计链前序关系。"""
        self._database_path = database_path
        self._schema_path = schema_path
        self._write_lock = asyncio.Lock()

    async def initialize(self) -> None:
        """创建本地审计表并为旧数据补齐哈希链字段，保证升级后的链可验证。"""
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        schema = self._schema_path.read_text(encoding="utf-8")

        def operation() -> None:
            """在独立连接中执行建表、字段迁移及历史审计事件哈希回填。"""
            with self._connect() as connection:
                connection.executescript(schema)
                columns = {
                    row["name"]
                    for row in connection.execute("PRAGMA table_info(audit_events)").fetchall()
                }
                needs_hash_backfill = "event_hash" not in columns
                if "previous_hash" not in columns:
                    connection.execute(
                        "ALTER TABLE audit_events ADD COLUMN previous_hash TEXT NOT NULL DEFAULT ''"
                    )
                if "event_hash" not in columns:
                    connection.execute(
                        "ALTER TABLE audit_events ADD COLUMN event_hash TEXT NOT NULL DEFAULT ''"
                    )
                if needs_hash_backfill:
                    previous_by_tenant: dict[str, str] = {}
                    rows = connection.execute(
                        "SELECT * FROM audit_events ORDER BY tenant_id, sequence"
                    ).fetchall()
                    for row in rows:
                        previous_hash = previous_by_tenant.get(row["tenant_id"], "")
                        canonical = _json(
                            {
                                "event_id": row["event_id"],
                                "tenant_id": row["tenant_id"],
                                "source_service": row["source_service"],
                                "event_type": row["event_type"],
                                "trace_id": row["trace_id"],
                                "occurred_at": row["occurred_at"],
                                "received_at": row["received_at"],
                                "payload": json.loads(row["payload_json"]),
                            }
                        )
                        event_hash = hashlib.sha256(
                            f"{previous_hash}:{canonical}".encode()
                        ).hexdigest()
                        connection.execute(
                            "UPDATE audit_events SET previous_hash = ?, event_hash = ? "
                            "WHERE sequence = ?",
                            (previous_hash, event_hash, row["sequence"]),
                        )
                        previous_by_tenant[row["tenant_id"]] = event_hash

        await asyncio.to_thread(operation)

    async def healthcheck(self) -> bool:
        """通过最小查询确认 SQLite 连接与基础读路径可用。"""
        return await self._read(
            lambda connection: connection.execute("SELECT 1").fetchone() is not None
        )

    async def ingest(self, event: GovernanceEvent, findings: list[Finding]) -> bool:
        """原子写入治理事件和派生发现，并将事件追加到所属租户的哈希链。"""

        def operation(connection: sqlite3.Connection) -> bool:
            """计算前序哈希、写入事件与发现；重复事件以唯一约束实现幂等拒绝。"""
            previous = connection.execute(
                "SELECT event_hash FROM audit_events WHERE tenant_id = ? "
                "ORDER BY sequence DESC LIMIT 1",
                (event.tenant_id,),
            ).fetchone()
            previous_hash = str(previous["event_hash"]) if previous else ""
            canonical = _json(
                {
                    "event_id": event.event_id,
                    "tenant_id": event.tenant_id,
                    "source_service": event.source_service,
                    "event_type": event.event_type,
                    "trace_id": event.trace_id,
                    "occurred_at": event.occurred_at.isoformat(),
                    "received_at": event.received_at.isoformat(),
                    "payload": event.payload,
                }
            )
            event_hash = hashlib.sha256(f"{previous_hash}:{canonical}".encode()).hexdigest()
            try:
                connection.execute(
                    """
                    INSERT INTO audit_events (
                        event_id, tenant_id, source_service, event_type, trace_id, occurred_at,
                        received_at, payload_json, previous_hash, event_hash
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event.event_id,
                        event.tenant_id,
                        event.source_service,
                        event.event_type,
                        event.trace_id,
                        event.occurred_at.isoformat(),
                        event.received_at.isoformat(),
                        _json(event.payload),
                        previous_hash,
                        event_hash,
                    ),
                )
            except sqlite3.IntegrityError:
                return False
            for finding in findings:
                connection.execute(
                    """
                    INSERT INTO findings (
                        finding_id, tenant_id, event_id, rule_id, severity, status, subject_type,
                        subject_id, summary, evidence_json, created_at, resolved_at, resolved_by,
                        resolution_note
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        finding.finding_id,
                        finding.tenant_id,
                        finding.event_id,
                        finding.rule_id,
                        finding.severity.value,
                        finding.status.value,
                        finding.subject_type,
                        finding.subject_id,
                        finding.summary,
                        _json(finding.evidence),
                        finding.created_at.isoformat(),
                        None,
                        None,
                        None,
                    ),
                )
            return True

        return await self._write(operation)

    async def verify_audit_chain(self, tenant_id: str) -> dict[str, Any]:
        """逐条重算指定租户审计链，定位前序哈希或事件哈希不一致的位置。"""

        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            """按序读取事件并基于规范化载荷验证链完整性。"""
            rows = connection.execute(
                "SELECT * FROM audit_events WHERE tenant_id = ? ORDER BY sequence ASC",
                (tenant_id,),
            ).fetchall()
            previous_hash = ""
            for row in rows:
                canonical = _json(
                    {
                        "event_id": row["event_id"],
                        "tenant_id": row["tenant_id"],
                        "source_service": row["source_service"],
                        "event_type": row["event_type"],
                        "trace_id": row["trace_id"],
                        "occurred_at": row["occurred_at"],
                        "received_at": row["received_at"],
                        "payload": json.loads(row["payload_json"]),
                    }
                )
                expected = hashlib.sha256(f"{previous_hash}:{canonical}".encode()).hexdigest()
                if row["previous_hash"] != previous_hash or row["event_hash"] != expected:
                    return {"valid": False, "events": len(rows), "brokenAt": row["event_id"]}
                previous_hash = expected
            return {"valid": True, "events": len(rows), "headHash": previous_hash}

        return await self._read(operation)

    async def list_audit_events(
        self, tenant_id: str, after_sequence: int, limit: int
    ) -> tuple[list[AuditEvent], int | None]:
        """按租户和游标分页读取审计事件，避免跨租户暴露审计记录。"""

        def operation(connection: sqlite3.Connection) -> tuple[list[AuditEvent], int | None]:
            """查询当前页事件并返回下一页游标所需的最后序号。"""
            rows = connection.execute(
                """SELECT * FROM audit_events WHERE tenant_id = ? AND sequence > ?
                   ORDER BY sequence ASC LIMIT ?""",
                (tenant_id, after_sequence, limit),
            ).fetchall()
            return [_audit_from_row(row) for row in rows], int(
                rows[-1]["sequence"]
            ) if rows else None

        return await self._read(operation)

    async def list_findings(
        self, tenant_id: str, status: FindingStatus | None, limit: int
    ) -> list[Finding]:
        """按租户、可选状态和数量上限列出治理发现。"""

        def operation(connection: sqlite3.Connection) -> list[Finding]:
            """构建受限查询并将数据库记录转换为领域发现模型。"""
            query = "SELECT * FROM findings WHERE tenant_id = ?"
            params: list[object] = [tenant_id]
            if status:
                query += " AND status = ?"
                params.append(status.value)
            query += " ORDER BY created_at DESC, finding_id DESC LIMIT ?"
            params.append(limit)
            return [_finding_from_row(row) for row in connection.execute(query, params).fetchall()]

        return await self._read(operation)

    async def resolve_finding(
        self, tenant_id: str, finding_id: str, resolved_by: str, note: str, timestamp: str
    ) -> Finding | None:
        """仅将指定租户仍处于 OPEN 状态的发现原子迁移为已解决。"""

        def operation(connection: sqlite3.Connection) -> Finding | None:
            """执行带状态条件的更新，防止并发重复解决覆盖已有处置人和说明。"""
            cursor = connection.execute(
                """
                UPDATE findings
                SET status = ?, resolved_at = ?, resolved_by = ?, resolution_note = ?
                WHERE tenant_id = ? AND finding_id = ? AND status = ?
                """,
                (
                    FindingStatus.RESOLVED.value,
                    timestamp,
                    resolved_by,
                    note,
                    tenant_id,
                    finding_id,
                    FindingStatus.OPEN.value,
                ),
            )
            if cursor.rowcount != 1:
                return None
            row = connection.execute(
                "SELECT * FROM findings WHERE tenant_id = ? AND finding_id = ?",
                (tenant_id, finding_id),
            ).fetchone()
            return _finding_from_row(row) if row else None

        return await self._write(operation)

    async def get_finding(self, tenant_id: str, finding_id: str) -> Finding | None:
        """按租户和发现标识读取单条发现，不存在时返回空。"""

        def operation(connection: sqlite3.Connection) -> Finding | None:
            """执行租户范围内的精确查询并恢复领域模型。"""
            row = connection.execute(
                "SELECT * FROM findings WHERE tenant_id = ? AND finding_id = ?",
                (tenant_id, finding_id),
            ).fetchone()
            return _finding_from_row(row) if row else None

        return await self._read(operation)

    async def get_tenant_policy(self, tenant_id: str) -> TenantPolicy | None:
        """读取租户治理策略；策略不存在时不虚构默认持久化记录。"""
        return await self._read(
            lambda connection: _policy_from_row(
                connection.execute(
                    "SELECT * FROM tenant_policies WHERE tenant_id = ?", (tenant_id,)
                ).fetchone()
            )
        )

    async def upsert_tenant_policy(self, policy: TenantPolicy, event: AuditEvent) -> None:
        """在同一写事务中更新租户策略并追加对应审计事件。"""
        await self._write(lambda connection: _upsert_policy_and_audit(connection, policy, event))

    async def report(
        self, tenant_id: str, from_time: str | None, to_time: str | None
    ) -> tuple[int, dict[str, int], list[Finding]]:
        """按时间窗汇总租户审计总量、来源分布及关联发现。"""

        def operation(connection: sqlite3.Connection) -> tuple[int, dict[str, int], list[Finding]]:
            """构建可选时间过滤条件并查询统计与关联发现明细。"""
            clauses = ["tenant_id = ?"]
            params: list[object] = [tenant_id]
            if from_time:
                clauses.append("occurred_at >= ?")
                params.append(from_time)
            if to_time:
                clauses.append("occurred_at <= ?")
                params.append(to_time)
            where = " AND ".join(clauses)
            total = int(
                connection.execute(
                    f"SELECT COUNT(*) FROM audit_events WHERE {where}", params
                ).fetchone()[0]
            )
            source_rows = connection.execute(
                f"""
                SELECT source_service, COUNT(*) AS count
                FROM audit_events WHERE {where}
                GROUP BY source_service
                """,
                params,
            ).fetchall()
            events_by_source = {
                str(row["source_service"]): int(row["count"]) for row in source_rows
            }
            finding_query = """
                SELECT findings.* FROM findings
                JOIN audit_events ON audit_events.event_id = findings.event_id
                WHERE audit_events.tenant_id = ?
            """
            finding_params: list[object] = [tenant_id]
            if from_time:
                finding_query += " AND audit_events.occurred_at >= ?"
                finding_params.append(from_time)
            if to_time:
                finding_query += " AND audit_events.occurred_at <= ?"
                finding_params.append(to_time)
            finding_rows = connection.execute(finding_query, finding_params).fetchall()
            return total, events_by_source, [_finding_from_row(row) for row in finding_rows]

        return await self._read(operation)

    async def upsert_document(
        self, tenant_id: str, kind: str, document_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """按租户、文档类别和标识写入治理文档，并维护更新时间。"""
        now = payload.get("updatedAt") or payload.get("createdAt")
        if not isinstance(now, str) or not now:
            from datetime import UTC, datetime

            now = datetime.now(UTC).isoformat()

        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            """使用冲突更新语义保存 JSON 载荷，避免重复文档产生多条记录。"""
            connection.execute(
                """
                INSERT INTO governance_documents (
                    tenant_id, kind, document_id, payload_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT (tenant_id, kind, document_id) DO UPDATE SET
                    payload_json = excluded.payload_json,
                    updated_at = excluded.updated_at
                """,
                (tenant_id, kind, document_id, _json(payload), now, now),
            )
            return payload

        return await self._write(operation)

    async def get_document(
        self, tenant_id: str, kind: str, document_id: str
    ) -> dict[str, Any] | None:
        """按租户、类别和文档标识读取原始治理文档载荷。"""

        def operation(connection: sqlite3.Connection) -> dict[str, Any] | None:
            """在租户边界内精确查询并反序列化 JSON 载荷。"""
            row = connection.execute(
                """
                SELECT payload_json FROM governance_documents
                WHERE tenant_id = ? AND kind = ? AND document_id = ?
                """,
                (tenant_id, kind, document_id),
            ).fetchone()
            return json.loads(row["payload_json"]) if row else None

        return await self._read(operation)

    async def list_documents(
        self, tenant_id: str, kind: str, limit: int = 1_000
    ) -> list[dict[str, Any]]:
        """按更新时间倒序列出租户指定类别的文档，并限制单次返回数量。"""

        def operation(connection: sqlite3.Connection) -> list[dict[str, Any]]:
            """执行受限列表查询并将每条 JSON 载荷还原为字典。"""
            rows = connection.execute(
                """
                SELECT payload_json FROM governance_documents
                WHERE tenant_id = ? AND kind = ?
                ORDER BY updated_at DESC, document_id DESC LIMIT ?
                """,
                (tenant_id, kind, limit),
            ).fetchall()
            return [json.loads(row["payload_json"]) for row in rows]

        return await self._read(operation)

    async def list_documents_all_tenants(
        self, kind: str, limit: int = 1_000
    ) -> list[dict[str, Any]]:
        """List service-owned work items across tenants for background workers only.

        This method is deliberately absent from request-facing services. Tenant-scoped
        APIs must continue to use :meth:`list_documents`; the cross-tenant read exists
        solely so a workload-identified relay can claim durable jobs.
        """

        def operation(connection: sqlite3.Connection) -> list[dict[str, Any]]:
            rows = connection.execute(
                """
                SELECT payload_json FROM governance_documents
                WHERE kind = ? ORDER BY updated_at ASC, document_id ASC LIMIT ?
                """,
                (kind, limit),
            ).fetchall()
            return [json.loads(row["payload_json"]) for row in rows]

        return await self._read(operation)

    async def compare_and_swap_document(
        self,
        tenant_id: str,
        kind: str,
        document_id: str,
        expected: dict[str, Any],
        replacement: dict[str, Any],
    ) -> bool:
        """Atomically replace a work item only if its complete payload is unchanged.

        The payload comparison is the repository's portable CAS token. It prevents
        two worker replicas from acquiring the same export without relying on a
        process-local lock and works identically through the PostgreSQL adapter.
        """
        from datetime import UTC, datetime

        def operation(connection: sqlite3.Connection) -> bool:
            cursor = connection.execute(
                """
                UPDATE governance_documents SET payload_json = ?, updated_at = ?
                WHERE tenant_id = ? AND kind = ? AND document_id = ? AND payload_json = ?
                """,
                (
                    _json(replacement),
                    datetime.now(UTC).isoformat(),
                    tenant_id,
                    kind,
                    document_id,
                    _json(expected),
                ),
            )
            return cursor.rowcount == 1

        return await self._write(operation)

    async def purge_documents_before(self, tenant_id: str, kinds: list[str], cutoff: str) -> int:
        """删除指定租户在保留截止时间之前、属于给定类别的文档。"""
        if not kinds:
            return 0

        def operation(connection: sqlite3.Connection) -> int:
            """构建参数化删除语句，避免类别集合直接拼接造成注入风险。"""
            placeholders = ",".join("?" for _ in kinds)
            cursor = connection.execute(
                f"DELETE FROM governance_documents WHERE tenant_id = ? "
                f"AND kind IN ({placeholders}) AND updated_at < ?",
                [tenant_id, *kinds, cutoff],
            )
            return cursor.rowcount

        return await self._write(operation)

    async def _read(self, operation: Callable[[sqlite3.Connection], T]) -> T:
        """在线程池中执行只读操作，避免阻塞事件循环且不获取写锁。"""

        def run() -> T:
            """打开短生命周期连接并运行调用方提供的只读函数。"""
            with self._connect() as connection:
                return operation(connection)

        return await asyncio.to_thread(run)

    async def _write(self, operation: Callable[[sqlite3.Connection], T]) -> T:
        """串行执行写操作并以 IMMEDIATE 事务包裹提交或回滚。"""
        async with self._write_lock:

            def run() -> T:
                """在独立连接中执行写入，异常时回滚以避免半完成状态。"""
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
        """创建启用外键约束和行字典访问方式的 SQLite 连接。"""
        connection = sqlite3.connect(self._database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection


class SqliteRepository(GovernanceRepositoryOperations):
    """开发/测试 SQLite 适配器；生产 PostgreSQL 不继承该具体后端。"""


def _json(value: object) -> str:
    """将载荷稳定序列化为规范 JSON，供哈希计算和持久化共用。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _audit_from_row(row: sqlite3.Row) -> AuditEvent:
    """将审计事件数据库行恢复为通过 Pydantic 校验的领域模型。"""
    return AuditEvent.model_validate(
        {
            "sequence": row["sequence"],
            "event_id": row["event_id"],
            "tenant_id": row["tenant_id"],
            "source_service": row["source_service"],
            "event_type": row["event_type"],
            "trace_id": row["trace_id"],
            "occurred_at": row["occurred_at"],
            "received_at": row["received_at"],
            "payload": json.loads(row["payload_json"]),
            "previous_hash": row["previous_hash"],
            "event_hash": row["event_hash"],
        }
    )


def _finding_from_row(row: sqlite3.Row) -> Finding:
    """将治理发现数据库行恢复为领域模型并反序列化证据字段。"""
    return Finding.model_validate(
        {
            "finding_id": row["finding_id"],
            "tenant_id": row["tenant_id"],
            "event_id": row["event_id"],
            "rule_id": row["rule_id"],
            "severity": row["severity"],
            "status": row["status"],
            "subject_type": row["subject_type"],
            "subject_id": row["subject_id"],
            "summary": row["summary"],
            "evidence": json.loads(row["evidence_json"]),
            "created_at": row["created_at"],
            "resolved_at": row["resolved_at"],
            "resolved_by": row["resolved_by"],
            "resolution_note": row["resolution_note"],
        }
    )


def _policy_from_row(row: sqlite3.Row | None) -> TenantPolicy | None:
    """在记录存在时将租户策略 JSON 恢复为领域模型。"""
    return TenantPolicy.model_validate_json(row["policy_json"]) if row else None


def _upsert_policy_and_audit(
    connection: sqlite3.Connection, policy: TenantPolicy, event: AuditEvent
) -> None:
    """在同一连接中写入策略快照和其审计事件，维持二者一致性。"""
    connection.execute(
        """
        INSERT INTO tenant_policies (tenant_id, policy_json, updated_by, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT (tenant_id) DO UPDATE SET policy_json = excluded.policy_json,
        updated_by = excluded.updated_by, updated_at = excluded.updated_at
        """,
        (
            policy.tenant_id,
            policy.model_dump_json(),
            policy.updated_by,
            policy.updated_at.isoformat(),
        ),
    )
    connection.execute(
        """
        INSERT INTO audit_events (
            event_id, tenant_id, source_service, event_type, trace_id, occurred_at,
            received_at, payload_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event.event_id,
            event.tenant_id,
            event.source_service,
            event.event_type,
            event.trace_id,
            event.occurred_at.isoformat(),
            event.received_at.isoformat(),
            _json(event.payload),
        ),
    )
