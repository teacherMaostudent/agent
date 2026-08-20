"""Transactional local persistence for approvals, idempotency and audit events.

The production PostgreSQL repository has the same ownership model.  Approval
consumption and idempotency claims are serialized so concurrent invocations
cannot duplicate a protected external side effect.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Any, Literal

from app.domain.errors import ApprovalError, IdempotencyConflictError
from app.domain.models import (
    ApprovalRecord,
    ApprovalStatus,
    AuditRecord,
    InvocationResponse,
    ToolExecutionState,
)


def _now() -> datetime:
    """生成带 UTC
    时区的当前时间，供状态记录、保留策略和审计排序使用，避免各调用方自行处理时区。
    """
    return datetime.now(UTC)


@dataclass(frozen=True)
class IdempotencyClaim:
    outcome: Literal["CLAIMED", "REPLAY"]
    response: InvocationResponse | None = None


class SqliteRepository:
    def __init__(self, path: Path) -> None:
        """打开本地 SQLite、启用 WAL 与外键约束并迁移 Outbox
        重试列；RLock 串行化同连接事务。 维护状态与审计/Outbox 一致性。
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self._lock = RLock()
        with self._lock:
            self.connection.execute("PRAGMA journal_mode=WAL")
            self.connection.execute("PRAGMA foreign_keys=ON")
            self.connection.executescript(_SCHEMA)
            outbox_columns = {
                row["name"]
                for row in self.connection.execute("PRAGMA table_info(event_outbox)").fetchall()
            }
            for statement, column in [
                (
                    "ALTER TABLE event_outbox ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0",
                    "attempts",
                ),
                ("ALTER TABLE event_outbox ADD COLUMN next_attempt_at TEXT", "next_attempt_at"),
                (
                    "ALTER TABLE event_outbox ADD COLUMN last_error TEXT NOT NULL DEFAULT ''",
                    "last_error",
                ),
            ]:
                if column not in outbox_columns:
                    self.connection.execute(statement)

    def ping(self) -> None:
        """在仓储锁内执行只读探活查询；失败使实例未就绪，不触发本地降级。
        与审计/Outbox 一致性。
        """
        with self._lock:
            self.connection.execute("SELECT 1").fetchone()

    def claim_idempotency(
        self,
        tenant_id: str,
        tool_name: str,
        key: str,
        request_hash: str,
        expires_at: datetime,
    ) -> IdempotencyClaim:
        """在 BEGIN IMMEDIATE
        事务中占用租户、工具和幂等键；同键异摘要冲突，同摘要已完成则返回可重放响应。
        与审计/Outbox 一致性。

        Atomically claim a key or return its completed response for replay.
        """
        now = _now().isoformat()
        with self._lock:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                self.connection.execute(
                    "DELETE FROM idempotency_records WHERE expires_at <= ?",
                    (now,),
                )
                row = self.connection.execute(
                    """
                    SELECT request_hash, status, response_json
                    FROM idempotency_records
                    WHERE tenant_id = ? AND tool_name = ? AND idempotency_key = ?
                    """,
                    (tenant_id, tool_name, key),
                ).fetchone()
                if row is not None:
                    if row["request_hash"] != request_hash:
                        raise IdempotencyConflictError(
                            "idempotency key was already used for a different request"
                        )
                    if row["status"] == "COMPLETED":
                        response = InvocationResponse.model_validate_json(row["response_json"])
                        self.connection.execute("COMMIT")
                        return IdempotencyClaim("REPLAY", response)
                    raise IdempotencyConflictError(
                        "an invocation with this idempotency key is already in progress"
                    )
                self.connection.execute(
                    """
                    INSERT INTO idempotency_records(
                        tenant_id, tool_name, idempotency_key, request_hash,
                        status, response_json, created_at, updated_at, expires_at
                    ) VALUES (?, ?, ?, ?, 'IN_PROGRESS', '', ?, ?, ?)
                    """,
                    (
                        tenant_id,
                        tool_name,
                        key,
                        request_hash,
                        now,
                        now,
                        expires_at.isoformat(),
                    ),
                )
                self.connection.execute("COMMIT")
                return IdempotencyClaim("CLAIMED")
            except Exception:
                self.connection.execute("ROLLBACK")
                raise

    def find_idempotency(
        self,
        tenant_id: str,
        tool_name: str,
        key: str,
        request_hash: str,
    ) -> InvocationResponse | None:
        """按租户、工具和幂等键读取已完成结果；请求摘要不一致时拒绝重放。 计/Outbox
        一致性。
        """
        now = _now().isoformat()
        with self._lock:
            self.connection.execute(
                "DELETE FROM idempotency_records WHERE expires_at <= ?",
                (now,),
            )
            row = self.connection.execute(
                """
                SELECT request_hash, status, response_json
                FROM idempotency_records
                WHERE tenant_id = ? AND tool_name = ? AND idempotency_key = ?
                """,
                (tenant_id, tool_name, key),
            ).fetchone()
        if row is None:
            return None
        if row["request_hash"] != request_hash:
            raise IdempotencyConflictError(
                "idempotency key was already used for a different request"
            )
        if row["status"] == "IN_PROGRESS":
            raise IdempotencyConflictError(
                "an invocation with this idempotency key is already in progress"
            )
        return InvocationResponse.model_validate_json(row["response_json"])

    def idempotency_state(self, tenant_id: str, tool_name: str, key: str) -> ToolExecutionState:
        """读取工具幂等记录的恢复状态; 查询不会创建、释放或执行任何工具。"""
        now = _now().isoformat()
        with self._lock:
            self.connection.execute("DELETE FROM idempotency_records WHERE expires_at <= ?", (now,))
            row = self.connection.execute(
                """
                SELECT status, response_json FROM idempotency_records
                WHERE tenant_id = ? AND tool_name = ? AND idempotency_key = ?
                """,
                (tenant_id, tool_name, key),
            ).fetchone()
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        if row is None:
            return ToolExecutionState(
                tool_name=tool_name, idempotency_key_sha256=digest, status="NOT_FOUND"
            )
        return ToolExecutionState(
            tool_name=tool_name,
            idempotency_key_sha256=digest,
            status=row["status"],
            response=(
                InvocationResponse.model_validate_json(row["response_json"])
                if row["status"] == "COMPLETED"
                else None
            ),
        )

    def complete_idempotency(
        self,
        tenant_id: str,
        tool_name: str,
        key: str,
        response: InvocationResponse,
    ) -> None:
        """仅把当前占用记录原子迁移为 COMPLETED
        并保存响应，避免并发调用覆盖结果。 与审计/Outbox 一致性。

        Apply the requested state transition with configured consistency checks.
        """
        with self._lock:
            self.connection.execute(
                """
                UPDATE idempotency_records
                SET status = 'COMPLETED', response_json = ?, updated_at = ?
                WHERE tenant_id = ? AND tool_name = ? AND idempotency_key = ?
                """,
                (
                    response.model_dump_json(),
                    _now().isoformat(),
                    tenant_id,
                    tool_name,
                    key,
                ),
            )

    def release_idempotency(self, tenant_id: str, tool_name: str, key: str) -> None:
        """删除失败调用留下的占用记录，使同一请求可安全重试；已完成记录不会被释放。
        计/Outbox 一致性。

        Release or remove owned state without bypassing cleanup rules.
        """
        with self._lock:
            self.connection.execute(
                """
                DELETE FROM idempotency_records
                WHERE tenant_id = ? AND tool_name = ? AND idempotency_key = ?
                  AND status = 'IN_PROGRESS'
                """,
                (tenant_id, tool_name, key),
            )

    def get_or_create_approval(
        self,
        record: ApprovalRecord,
    ) -> ApprovalRecord:
        """按请求摘要复用仍有效的待审批记录，否则创建与租户、工具版本和参数摘要绑定的新审批
        。 Outbox 一致性。

        Return the requested value through the established ownership boundary.
        """
        now = _now()
        with self._lock:
            self._expire_approvals(now)
            row = self.connection.execute(
                """
                SELECT * FROM approvals
                WHERE tenant_id = ? AND user_id = ? AND tool_name = ?
                  AND tool_version = ? AND request_hash = ?
                  AND status IN ('PENDING', 'APPROVED')
                ORDER BY requested_at DESC
                LIMIT 1
                """,
                (
                    record.tenant_id,
                    record.user_id,
                    record.tool_name,
                    record.tool_version,
                    record.request_hash,
                ),
            ).fetchone()
            if row is not None:
                return self._approval_from_row(row)
            self.connection.execute(
                """
                INSERT INTO approvals(
                    approval_id, tenant_id, user_id, tool_name, tool_version,
                    request_hash, status, reason, requested_at, expires_at,
                    decided_by, decided_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', NULL)
                """,
                (
                    record.approval_id,
                    record.tenant_id,
                    record.user_id,
                    record.tool_name,
                    record.tool_version,
                    record.request_hash,
                    record.status.value,
                    record.reason,
                    record.requested_at.isoformat(),
                    record.expires_at.isoformat(),
                ),
            )
            return record

    def get_approval(self, approval_id: str) -> ApprovalRecord | None:
        """读取审批并先刷新过期状态；该方法不批准、不拒绝也不消费授权。 Outbox
        一致性。

        Return the requested value through the established ownership boundary.
        """
        with self._lock:
            self._expire_approvals(_now())
            row = self.connection.execute(
                "SELECT * FROM approvals WHERE approval_id = ?",
                (approval_id,),
            ).fetchone()
            return None if row is None else self._approval_from_row(row)

    def decide_approval(
        self,
        approval_id: str,
        tenant_id: str,
        *,
        approved: bool,
        decided_by: str,
        reason: str,
    ) -> ApprovalRecord:
        """在租户约束下把 PENDING 审批迁移为 APPROVED 或
        REJECTED，并保存审批人、原因和时间。 Outbox 一致性。
        """
        now = _now()
        with self._lock:
            self._expire_approvals(now)
            row = self.connection.execute(
                "SELECT * FROM approvals WHERE approval_id = ? AND tenant_id = ?",
                (approval_id, tenant_id),
            ).fetchone()
            if row is None:
                raise ApprovalError("approval does not exist")
            record = self._approval_from_row(row)
            if record.status != ApprovalStatus.PENDING:
                raise ApprovalError(f"approval is not pending: {record.status}")
            status = ApprovalStatus.APPROVED if approved else ApprovalStatus.REJECTED
            self.connection.execute(
                """
                UPDATE approvals
                SET status = ?, reason = ?, decided_by = ?, decided_at = ?
                WHERE approval_id = ?
                """,
                (status.value, reason, decided_by, now.isoformat(), approval_id),
            )
            updated = self.connection.execute(
                "SELECT * FROM approvals WHERE approval_id = ?",
                (approval_id,),
            ).fetchone()
            return self._approval_from_row(updated)

    def consume_approval(self, approval_id: str) -> None:
        """以条件更新把 APPROVED 原子迁移为
        CONSUMED；更新行数不为一表示授权已用或状态非法。 审计/Outbox
        一致性。

        Apply the requested state transition with configured consistency checks.
        """
        with self._lock:
            cursor = self.connection.execute(
                """
                UPDATE approvals SET status = 'CONSUMED'
                WHERE approval_id = ? AND status = 'APPROVED'
                """,
                (approval_id,),
            )
            if cursor.rowcount != 1:
                raise ApprovalError("approval was already consumed")

    def append_audit(self, record: AuditRecord) -> None:
        """追加一次不可变工具执行审计，保存参数和幂等键摘要而不是敏感原文。 Outbox
        一致性。
        """
        with self._lock:
            self.connection.execute(
                """
                INSERT INTO audit_records(
                    audit_id, invocation_id, request_id, tenant_id, user_id,
                    tool_name, tool_version, status, attempt_count, duration_ms,
                    arguments_sha256, idempotency_key_sha256, error_type, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.audit_id,
                    record.invocation_id,
                    record.request_id,
                    record.tenant_id,
                    record.user_id,
                    record.tool_name,
                    record.tool_version,
                    record.status.value,
                    record.attempt_count,
                    record.duration_ms,
                    record.arguments_sha256,
                    record.idempotency_key_sha256,
                    record.error_type,
                    record.created_at.isoformat(),
                ),
            )

    def enqueue_event(self, event: dict[str, Any]) -> None:
        """把规范化治理事件幂等写入本地 Outbox；重复 event_id
        不产生第二条消息。 护状态与审计/Outbox 一致性。
        """
        with self._lock:
            self.connection.execute(
                "INSERT INTO event_outbox"
                "(event_id, payload_json, created_at) VALUES (?, ?, ?) "
                "ON CONFLICT(event_id) DO NOTHING",
                (event["event_id"], json.dumps(event, ensure_ascii=False), _now().isoformat()),
            )

    def pending_events(self, limit: int = 100) -> list[dict[str, Any]]:
        """按 next_attempt_at 和数量上限读取待投递 Outbox
        事件；读取本身不增加尝试次数。 在事务内同步维护状态与审计/Outbox
        一致性。
        """
        with self._lock:
            rows = self.connection.execute(
                "SELECT payload_json FROM event_outbox WHERE delivered_at IS NULL "
                "AND (next_attempt_at IS NULL OR next_attempt_at <= ?) ORDER BY created_at LIMIT ?",
                (_now().isoformat(), limit),
            ).fetchall()
        return [json.loads(row["payload_json"]) for row in rows]

    def mark_event_delivered(self, event_id: str) -> None:
        """在消息代理确认后记录 Outbox 投递时间；业务工具状态保持不变。
        Outbox 一致性。
        """
        with self._lock:
            self.connection.execute(
                "UPDATE event_outbox SET delivered_at = ? WHERE event_id = ?",
                (_now().isoformat(), event_id),
            )

    def mark_event_failed(self, event_id: str, error: str) -> None:
        """记录投递错误、增加尝试次数并计算指数退避时间，事件仍保留在 Outbox。
        Outbox 一致性。
        """
        from datetime import timedelta

        with self._lock:
            row = self.connection.execute(
                "SELECT attempts FROM event_outbox WHERE event_id = ?", (event_id,)
            ).fetchone()
            attempts = int(row["attempts"]) + 1 if row else 1
            next_attempt = _now() + timedelta(seconds=min(900, 2 ** min(attempts, 9)))
            self.connection.execute(
                "UPDATE event_outbox SET attempts = ?, next_attempt_at = ?, "
                "last_error = ? WHERE event_id = ?",
                (attempts, next_attempt.isoformat(), error[:1000], event_id),
            )

    def list_audit(
        self,
        tenant_id: str,
        *,
        tool_name: str | None = None,
        limit: int = 100,
    ) -> list[AuditRecord]:
        """按租户、时间游标和数量上限分页读取审计记录，禁止无租户全表查询。 Outbox
        一致性。

        List only values visible within the caller's tenant and lifecycle scope.
        """
        sql = "SELECT * FROM audit_records WHERE tenant_id = ?"
        values: list[object] = [tenant_id]
        if tool_name:
            sql += " AND tool_name = ?"
            values.append(tool_name)
        sql += " ORDER BY created_at DESC LIMIT ?"
        values.append(limit)
        with self._lock:
            rows = self.connection.execute(sql, values).fetchall()
        return [
            AuditRecord(
                audit_id=row["audit_id"],
                invocation_id=row["invocation_id"],
                request_id=row["request_id"],
                tenant_id=row["tenant_id"],
                user_id=row["user_id"],
                tool_name=row["tool_name"],
                tool_version=row["tool_version"],
                status=row["status"],
                attempt_count=row["attempt_count"],
                duration_ms=row["duration_ms"],
                arguments_sha256=row["arguments_sha256"],
                idempotency_key_sha256=row["idempotency_key_sha256"],
                error_type=row["error_type"],
                created_at=datetime.fromisoformat(row["created_at"]),
            )
            for row in rows
        ]

    def close(self) -> None:
        """在仓储锁内关闭 SQLite 连接；不提交未完成的工具执行或审批事务。
        态与审计/Outbox 一致性。
        """
        with self._lock:
            self.connection.close()

    def _expire_approvals(self, now: datetime) -> None:
        """把超过有效期且仍为待处理的审批转为 EXPIRED，确保过期授权不能被消费。
        ；写操作在事务内同步维护状态与审计/Outbox 一致性。
        """
        self.connection.execute(
            """
            UPDATE approvals SET status = 'EXPIRED'
            WHERE status IN ('PENDING', 'APPROVED') AND expires_at <= ?
            """,
            (now.isoformat(),),
        )

    @staticmethod
    def _approval_from_row(row: sqlite3.Row) -> ApprovalRecord:
        """把数据库行显式映射为审批记录领域模型；通过模型校验阻止损坏或旧格式数据进入应用层
        。
        """
        return ApprovalRecord(
            approval_id=row["approval_id"],
            tenant_id=row["tenant_id"],
            user_id=row["user_id"],
            tool_name=row["tool_name"],
            tool_version=row["tool_version"],
            request_hash=row["request_hash"],
            status=row["status"],
            reason=row["reason"],
            requested_at=datetime.fromisoformat(row["requested_at"]),
            expires_at=datetime.fromisoformat(row["expires_at"]),
            decided_by=row["decided_by"],
            decided_at=(datetime.fromisoformat(row["decided_at"]) if row["decided_at"] else None),
        )


_SCHEMA = """
CREATE TABLE IF NOT EXISTS idempotency_records (
    tenant_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, tool_name, idempotency_key)
);

CREATE TABLE IF NOT EXISTS approvals (
    approval_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    tool_version TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    reason TEXT NOT NULL,
    requested_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    decided_by TEXT NOT NULL,
    decided_at TEXT
);
CREATE INDEX IF NOT EXISTS approvals_request_idx
ON approvals(tenant_id, user_id, tool_name, tool_version, request_hash, status);

CREATE TABLE IF NOT EXISTS audit_records (
    audit_id TEXT PRIMARY KEY,
    invocation_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    tool_version TEXT NOT NULL,
    status TEXT NOT NULL,
    attempt_count INTEGER NOT NULL,
    duration_ms INTEGER NOT NULL,
    arguments_sha256 TEXT NOT NULL,
    idempotency_key_sha256 TEXT NOT NULL,
    error_type TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS audit_tenant_created_idx
ON audit_records(tenant_id, created_at DESC);

CREATE TABLE IF NOT EXISTS event_outbox (
    event_id TEXT PRIMARY KEY,
    payload_json TEXT NOT NULL,
    delivered_at TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT,
    last_error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
"""
