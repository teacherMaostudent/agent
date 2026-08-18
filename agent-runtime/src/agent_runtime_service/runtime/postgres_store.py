from __future__ import annotations

import re

from platform_infra.postgres import connect_postgres, execute_script
from platform_infra.schema_registry import SchemaRegistry

from agent_runtime_service.runtime.integration import RuntimeStoreOperations

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runtime_runs(
    run_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, user_id TEXT NOT NULL,
    agent_id TEXT NOT NULL, snapshot_id TEXT NOT NULL, request_id TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL, runtime_state TEXT NOT NULL DEFAULT 'CREATED', context_json TEXT NOT NULL, result_json TEXT NOT NULL,
    error_code TEXT NOT NULL, cancel_requested SMALLINT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS runtime_runs_request_id_idx
    ON runtime_runs(tenant_id, request_id) WHERE request_id <> '';
ALTER TABLE runtime_runs ADD COLUMN IF NOT EXISTS runtime_state TEXT NOT NULL DEFAULT 'CREATED';
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
    turn_id TEXT NOT NULL DEFAULT '', step_id TEXT NOT NULL DEFAULT '',
    epoch_id TEXT NOT NULL DEFAULT '', attempt_id TEXT NOT NULL DEFAULT '',
    payload_version TEXT NOT NULL DEFAULT 'session-event/v1',
    metadata_json TEXT NOT NULL, occurred_at TIMESTAMPTZ NOT NULL,
    UNIQUE (tenant_id, session_id, sequence)
);
CREATE INDEX IF NOT EXISTS runtime_session_events_lookup_idx
    ON runtime_session_events(tenant_id, session_id, sequence);
CREATE TABLE IF NOT EXISTS runtime_sessions(
    tenant_id TEXT NOT NULL, session_id TEXT NOT NULL, header_json TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL, PRIMARY KEY(tenant_id, session_id)
);
CREATE TABLE IF NOT EXISTS runtime_session_projections(
    tenant_id TEXT NOT NULL, session_id TEXT NOT NULL, projection_json TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL, PRIMARY KEY(tenant_id, session_id)
);
CREATE TABLE IF NOT EXISTS runtime_session_archives(
    tenant_id TEXT NOT NULL, session_id TEXT NOT NULL, archive_key TEXT NOT NULL,
    archive_sha256 TEXT NOT NULL, archived_through_sequence INTEGER NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY(tenant_id, session_id, archived_through_sequence)
);
CREATE TABLE IF NOT EXISTS runtime_run_mailbox(
    message_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, run_id TEXT NOT NULL,
    input_type TEXT NOT NULL, idempotency_key TEXT NOT NULL,
    control_json TEXT NOT NULL DEFAULT '{}',
    priority INTEGER NOT NULL DEFAULT 50,
    delivery_status TEXT NOT NULL DEFAULT 'PENDING', lease_token TEXT NOT NULL DEFAULT '',
    lease_expires_at TIMESTAMPTZ, created_at TIMESTAMPTZ NOT NULL, consumed_at TIMESTAMPTZ,
    UNIQUE(tenant_id, run_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS runtime_run_mailbox_claim_idx
    ON runtime_run_mailbox(tenant_id, run_id, delivery_status, priority, created_at);
CREATE TABLE IF NOT EXISTS runtime_root_budgets(
    tenant_id TEXT NOT NULL, root_task_id TEXT NOT NULL,
    max_cost_usd DOUBLE PRECISION NOT NULL, max_steps INTEGER NOT NULL,
    reserved_cost_usd DOUBLE PRECISION NOT NULL DEFAULT 0, reserved_steps INTEGER NOT NULL DEFAULT 0,
    spent_cost_usd DOUBLE PRECISION NOT NULL DEFAULT 0, consumed_steps INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY(tenant_id, root_task_id)
);
CREATE TABLE IF NOT EXISTS runtime_root_budget_reservations(
    reservation_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, root_task_id TEXT NOT NULL,
    run_id TEXT NOT NULL, reserved_cost_usd DOUBLE PRECISION NOT NULL, reserved_steps INTEGER NOT NULL,
    settled_cost_usd DOUBLE PRECISION, settled_steps INTEGER, created_at TIMESTAMPTZ NOT NULL,
    settled_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS runtime_root_budget_reservations_lookup_idx
    ON runtime_root_budget_reservations(tenant_id, root_task_id, run_id);
ALTER TABLE runtime_run_mailbox ADD COLUMN IF NOT EXISTS priority INTEGER NOT NULL DEFAULT 50;
ALTER TABLE runtime_run_mailbox ADD COLUMN IF NOT EXISTS control_json TEXT NOT NULL DEFAULT '{}';
ALTER TABLE runtime_session_events ADD COLUMN IF NOT EXISTS parent_run_id TEXT NOT NULL DEFAULT '';
ALTER TABLE runtime_session_events ADD COLUMN IF NOT EXISTS turn_id TEXT NOT NULL DEFAULT '';
ALTER TABLE runtime_session_events ADD COLUMN IF NOT EXISTS step_id TEXT NOT NULL DEFAULT '';
ALTER TABLE runtime_session_events ADD COLUMN IF NOT EXISTS epoch_id TEXT NOT NULL DEFAULT '';
ALTER TABLE runtime_session_events ADD COLUMN IF NOT EXISTS attempt_id TEXT NOT NULL DEFAULT '';
ALTER TABLE runtime_session_events ADD COLUMN IF NOT EXISTS payload_version TEXT NOT NULL DEFAULT 'session-event/v1';
"""


class PostgresRuntimeStore(RuntimeStoreOperations):
    def __init__(self, dsn: str, schema: str, schema_registry: SchemaRegistry | None = None) -> None:
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
        self._schema_registry = schema_registry
        execute_script(self._connection, _SCHEMA)
        self._connection.commit()

    def _lock_session_stream(self, tenant_id: str, session_id: str) -> None:
        """使用事务级咨询锁串行化同一会话的序号分配，防止多 Runtime 副本产生序号竞争。"""
        self._connection.execute(
            "SELECT pg_advisory_xact_lock(hashtext(?), hashtext(?))",
            (tenant_id, session_id),
        )
