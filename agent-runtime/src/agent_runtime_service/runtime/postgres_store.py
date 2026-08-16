from __future__ import annotations

import re

from platform_infra.postgres import connect_postgres, execute_script

from agent_runtime_service.runtime.integration import RuntimeStore

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runtime_runs(
    run_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, user_id TEXT NOT NULL,
    agent_id TEXT NOT NULL, snapshot_id TEXT NOT NULL, request_id TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL, context_json TEXT NOT NULL, result_json TEXT NOT NULL,
    error_code TEXT NOT NULL, cancel_requested SMALLINT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS runtime_runs_request_id_idx
    ON runtime_runs(tenant_id, request_id) WHERE request_id <> '';
CREATE TABLE IF NOT EXISTS runtime_outbox(
    event_id TEXT PRIMARY KEY, payload_json TEXT NOT NULL, delivered_at TIMESTAMPTZ,
    attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at TIMESTAMPTZ,
    last_error TEXT NOT NULL DEFAULT '', created_at TIMESTAMPTZ NOT NULL
);
CREATE TABLE IF NOT EXISTS runtime_session_events(
    event_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, session_id TEXT NOT NULL,
    run_id TEXT NOT NULL, parent_run_id TEXT NOT NULL DEFAULT '', trace_id TEXT NOT NULL, agent_id TEXT NOT NULL,
    snapshot_id TEXT NOT NULL, sequence INTEGER NOT NULL, event_type TEXT NOT NULL,
    status TEXT NOT NULL, error_code TEXT NOT NULL DEFAULT '',
    metadata_json TEXT NOT NULL, occurred_at TIMESTAMPTZ NOT NULL,
    UNIQUE (tenant_id, session_id, sequence)
);
CREATE INDEX IF NOT EXISTS runtime_session_events_lookup_idx
    ON runtime_session_events(tenant_id, session_id, sequence);
ALTER TABLE runtime_session_events ADD COLUMN IF NOT EXISTS parent_run_id TEXT NOT NULL DEFAULT '';
"""


class PostgresRuntimeStore(RuntimeStore):
    def __init__(self, dsn: str, schema: str) -> None:
        """初始化生产 PostgreSQL Run/Outbox 存储并校验 schema 名，防止 SQL 标识符注入。"""
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", schema):
            raise ValueError("invalid PostgreSQL schema")
        self._dsn = dsn
        self._schema = schema
        from threading import Lock

        self._lock = Lock()
        with connect_postgres(dsn, schema) as connection:
            connection.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
            connection.commit()
        self._connection = connect_postgres(dsn, schema)
        execute_script(self._connection, _SCHEMA)
        self._connection.commit()

    def _lock_session_stream(self, tenant_id: str, session_id: str) -> None:
        """使用事务级咨询锁串行化同一会话的序号分配，防止多 Runtime 副本产生序号竞争。"""
        self._connection.execute(
            "SELECT pg_advisory_xact_lock(hashtext(?), hashtext(?))",
            (tenant_id, session_id),
        )
