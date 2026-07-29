from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

from app.domain.models import AuditEvent, Finding, FindingStatus, GovernanceEvent, TenantPolicy

T = TypeVar("T")


class SqliteRepository:
    def __init__(self, database_path: Path, schema_path: Path) -> None:
        self._database_path = database_path
        self._schema_path = schema_path
        self._write_lock = asyncio.Lock()

    async def initialize(self) -> None:
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        schema = self._schema_path.read_text(encoding="utf-8")

        def operation() -> None:
            with self._connect() as connection:
                connection.executescript(schema)

        await asyncio.to_thread(operation)

    async def healthcheck(self) -> bool:
        return await self._read(
            lambda connection: connection.execute("SELECT 1").fetchone() is not None
        )

    async def ingest(self, event: GovernanceEvent, findings: list[Finding]) -> bool:
        def operation(connection: sqlite3.Connection) -> bool:
            try:
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

    async def list_audit_events(
        self, tenant_id: str, after_sequence: int, limit: int
    ) -> tuple[list[AuditEvent], int | None]:
        def operation(connection: sqlite3.Connection) -> tuple[list[AuditEvent], int | None]:
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
        def operation(connection: sqlite3.Connection) -> list[Finding]:
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
        def operation(connection: sqlite3.Connection) -> Finding | None:
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
        def operation(connection: sqlite3.Connection) -> Finding | None:
            row = connection.execute(
                "SELECT * FROM findings WHERE tenant_id = ? AND finding_id = ?",
                (tenant_id, finding_id),
            ).fetchone()
            return _finding_from_row(row) if row else None

        return await self._read(operation)

    async def get_tenant_policy(self, tenant_id: str) -> TenantPolicy | None:
        return await self._read(
            lambda connection: _policy_from_row(
                connection.execute(
                    "SELECT * FROM tenant_policies WHERE tenant_id = ?", (tenant_id,)
                ).fetchone()
            )
        )

    async def upsert_tenant_policy(self, policy: TenantPolicy, event: AuditEvent) -> None:
        await self._write(lambda connection: _upsert_policy_and_audit(connection, policy, event))

    async def report(
        self, tenant_id: str, from_time: str | None, to_time: str | None
    ) -> tuple[int, dict[str, int], list[Finding]]:
        def operation(connection: sqlite3.Connection) -> tuple[int, dict[str, int], list[Finding]]:
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
        now = payload.get("updatedAt") or payload.get("createdAt")
        if not isinstance(now, str) or not now:
            from datetime import UTC, datetime

            now = datetime.now(UTC).isoformat()

        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
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
        def operation(connection: sqlite3.Connection) -> dict[str, Any] | None:
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
        def operation(connection: sqlite3.Connection) -> list[dict[str, Any]]:
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

    async def _read(self, operation: Callable[[sqlite3.Connection], T]) -> T:
        def run() -> T:
            with self._connect() as connection:
                return operation(connection)

        return await asyncio.to_thread(run)

    async def _write(self, operation: Callable[[sqlite3.Connection], T]) -> T:
        async with self._write_lock:

            def run() -> T:
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
        connection = sqlite3.connect(self._database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _audit_from_row(row: sqlite3.Row) -> AuditEvent:
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
        }
    )


def _finding_from_row(row: sqlite3.Row) -> Finding:
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
    return TenantPolicy.model_validate_json(row["policy_json"]) if row else None


def _upsert_policy_and_audit(
    connection: sqlite3.Connection, policy: TenantPolicy, event: AuditEvent
) -> None:
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
