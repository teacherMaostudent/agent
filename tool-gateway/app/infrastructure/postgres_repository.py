from __future__ import annotations

import re
from threading import RLock

from platform_infra.postgres import connect_postgres, execute_script

from app.infrastructure.repository import SqliteRepository

_SCHEMA = """
CREATE TABLE IF NOT EXISTS idempotency_records(
    tenant_id TEXT NOT NULL, tool_name TEXT NOT NULL, idempotency_key TEXT NOT NULL,
    request_hash TEXT NOT NULL, status TEXT NOT NULL, response_json TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY(tenant_id, tool_name, idempotency_key)
);
CREATE TABLE IF NOT EXISTS approvals(
    approval_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, user_id TEXT NOT NULL,
    tool_name TEXT NOT NULL, tool_version TEXT NOT NULL, request_hash TEXT NOT NULL,
    status TEXT NOT NULL, reason TEXT NOT NULL, requested_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL, decided_by TEXT NOT NULL, decided_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS approvals_request_idx
ON approvals(tenant_id, user_id, tool_name, tool_version, request_hash, status);
CREATE TABLE IF NOT EXISTS audit_records(
    audit_id TEXT PRIMARY KEY, invocation_id TEXT NOT NULL, request_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL, user_id TEXT NOT NULL, tool_name TEXT NOT NULL,
    tool_version TEXT NOT NULL, status TEXT NOT NULL, attempt_count INTEGER NOT NULL,
    duration_ms INTEGER NOT NULL, arguments_sha256 TEXT NOT NULL,
    idempotency_key_sha256 TEXT NOT NULL, error_type TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS audit_tenant_created_idx
ON audit_records(tenant_id, created_at DESC);
CREATE TABLE IF NOT EXISTS event_outbox(
    event_id TEXT PRIMARY KEY, payload_json TEXT NOT NULL, delivered_at TIMESTAMPTZ,
    attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at TIMESTAMPTZ,
    last_error TEXT NOT NULL DEFAULT '', created_at TIMESTAMPTZ NOT NULL
);
"""


class PostgresRepository(SqliteRepository):
    def __init__(self, dsn: str, schema: str) -> None:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", schema):
            raise ValueError("invalid PostgreSQL schema")
        self._lock = RLock()
        with connect_postgres(dsn, schema) as connection:
            connection.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
            connection.commit()
        self.connection = connect_postgres(dsn, schema)
        execute_script(self.connection, _SCHEMA)
        self.connection.commit()
