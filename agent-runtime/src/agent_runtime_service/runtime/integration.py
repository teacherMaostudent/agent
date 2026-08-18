"""Runtime clients and durable run/outbox state.

Control Plane resolution is an authenticated cross-service boundary.  Runtime
events are persisted with run state so a temporary Governance outage cannot
erase an execution outcome.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Lock
from typing import Any
from uuid import uuid4

import httpx
from platform_infra.identity import WorkloadTokenProvider
from platform_infra.schema_registry import SchemaRegistry
from platform_sdk.contracts.execution import ExecutionContext, RuntimeRun

from agent_runtime_service.runtime.mailbox import (
    AgentInputPriority,
    ClaimedRunMailboxItem,
    RunMailboxInputType,
)
from agent_runtime_service.runtime.run_state import (
    AgentRunEvent,
    AgentRunState,
    InvalidRunTransition,
    transition_run_state,
)
from agent_runtime_service.runtime.session_events import (
    ModelVisibleMessage,
    RuntimeEventType,
    RuntimeLifecycleEvent,
    SessionHeader,
    SessionProjection,
    derive_model_messages,
    derive_session_projection,
    event_type_for_status,
)


def _governance_event_for_state_change(
    context: ExecutionContext, lifecycle_event: RuntimeLifecycleEvent
) -> dict[str, Any]:
    """将无正文的 Run 状态事实投影为可由 Governance 关联的审计事件。

    事件 ID 从 Session Ledger 的事实 ID 推导，因此同一事务重试不会产生第二条
    治理记录。此处不包含用户消息、Prompt、工具返回或模型文本，避免治理 Outbox
    成为跨数据域的敏感内容副本。
    """
    metadata = lifecycle_event.metadata
    return {
        "event_id": f"gov_{lifecycle_event.event_id}",
        "source_service": "agent-runtime",
        "event_type": "agent.run.state_changed",
        "trace_id": context.trace_id,
        "tenant_id": context.tenant_id,
        "occurred_at": lifecycle_event.occurred_at.isoformat(),
        "payload": {
            "run_id": context.run_id,
            "session_id": context.session_id,
            "agent_id": context.agent_id,
            "snapshot_id": context.snapshot_id,
            "agent_version": context.agent_version,
            "status": lifecycle_event.status,
            "runtime_state": metadata.get("current_state"),
            "previous_runtime_state": metadata.get("previous_state"),
            "transition_event": metadata.get("trigger"),
            "session_event_id": lifecycle_event.event_id,
            "sequence": lifecycle_event.sequence,
        },
    }


class ControlPlaneClient:
    def __init__(
        self,
        base_url: str,
        runtime_key: str,
        timeout: float,
        workload_identity: WorkloadTokenProvider | None = None,
        mtls: dict[str, Any] | None = None,
    ) -> None:
        """配置 Control Plane 解析客户端；快照身份由服务认证与可选 mTLS 保护。"""
        self.base_url = base_url.rstrip("/")
        self.runtime_key = runtime_key
        self.timeout = timeout
        self.workload_identity = workload_identity
        self.mtls = mtls or {}

    def resolve(
        self,
        *,
        tenant_id: str,
        user_id: str,
        agent_id: str,
        environment: str,
        session_id: str,
        trace_id: str,
    ) -> dict[str, Any]:
        """解析租户运行的不可变快照；HTTP 失败必须阻止运行而非回退草稿。"""
        headers = {
            "X-Tenant-Id": tenant_id,
            "X-User-Id": user_id,
            "X-Trace-Id": trace_id,
        }
        if self.runtime_key:
            headers["X-Runtime-Key"] = self.runtime_key
        if self.workload_identity is not None:
            headers.update(self.workload_identity.authorization_header())
        response = httpx.get(
            f"{self.base_url}/v1/runtime/agents/{agent_id}/resolve",
            params={"environment": environment, "session_id": session_id},
            headers=headers,
            timeout=self.timeout,
            **self.mtls,
        )
        response.raise_for_status()
        return response.json()


class RuntimeStoreOperations:
    """与持久化方言无关的 Run、Outbox 与 Session Ledger 操作。"""
    """Small durable Run + transactional-outbox store; PostgreSQL is the production adapter."""

    def __init__(self, path: Path, schema_registry: SchemaRegistry | None = None) -> None:
        """初始化本地 Run 与 Transactional Outbox 存储，仅作 PostgreSQL 的开发替身。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._lock = Lock()
        self._schema_registry = schema_registry
        with self._lock:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS runtime_runs (
                    run_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, user_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL, snapshot_id TEXT NOT NULL, request_id TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL, runtime_state TEXT NOT NULL DEFAULT 'CREATED',
                    context_json TEXT NOT NULL, result_json TEXT NOT NULL, error_code TEXT NOT NULL,
                    cancel_requested INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS runtime_outbox (
                    event_id TEXT PRIMARY KEY, payload_json TEXT NOT NULL, delivered_at TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at TEXT,
                    last_error TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS runtime_session_events (
                    event_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, session_id TEXT NOT NULL,
                    run_id TEXT NOT NULL, parent_run_id TEXT NOT NULL DEFAULT '', trace_id TEXT NOT NULL, agent_id TEXT NOT NULL,
                    snapshot_id TEXT NOT NULL, sequence INTEGER NOT NULL, event_type TEXT NOT NULL,
                    status TEXT NOT NULL, error_code TEXT NOT NULL DEFAULT '',
                    turn_id TEXT NOT NULL DEFAULT '', step_id TEXT NOT NULL DEFAULT '',
                    epoch_id TEXT NOT NULL DEFAULT '', attempt_id TEXT NOT NULL DEFAULT '',
                    payload_version TEXT NOT NULL DEFAULT 'session-event/v1',
                    metadata_json TEXT NOT NULL, occurred_at TEXT NOT NULL,
                    UNIQUE (tenant_id, session_id, sequence)
                );
                CREATE INDEX IF NOT EXISTS runtime_session_events_lookup_idx
                    ON runtime_session_events(tenant_id, session_id, sequence);
                CREATE TABLE IF NOT EXISTS runtime_sessions (
                    tenant_id TEXT NOT NULL, session_id TEXT NOT NULL,
                    header_json TEXT NOT NULL, created_at TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, session_id)
                );
                CREATE TABLE IF NOT EXISTS runtime_session_projections (
                    tenant_id TEXT NOT NULL, session_id TEXT NOT NULL,
                    projection_json TEXT NOT NULL, updated_at TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, session_id)
                );
                CREATE TABLE IF NOT EXISTS runtime_session_archives (
                    tenant_id TEXT NOT NULL, session_id TEXT NOT NULL, archive_key TEXT NOT NULL,
                    archive_sha256 TEXT NOT NULL, archived_through_sequence INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, session_id, archived_through_sequence)
                );
                CREATE TABLE IF NOT EXISTS runtime_run_mailbox (
                    message_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, run_id TEXT NOT NULL,
                    input_type TEXT NOT NULL, idempotency_key TEXT NOT NULL,
                    priority INTEGER NOT NULL DEFAULT 50,
                    delivery_status TEXT NOT NULL DEFAULT 'PENDING', lease_token TEXT NOT NULL DEFAULT '',
                    lease_expires_at TEXT, created_at TEXT NOT NULL, consumed_at TEXT,
                    UNIQUE(tenant_id, run_id, idempotency_key)
                );
                CREATE INDEX IF NOT EXISTS runtime_run_mailbox_claim_idx
                    ON runtime_run_mailbox(tenant_id, run_id, delivery_status, priority, created_at);
                """
            )
            columns = {
                row["name"]
                for row in self._connection.execute("PRAGMA table_info(runtime_runs)").fetchall()
            }
            if "request_id" not in columns:
                self._connection.execute(
                    "ALTER TABLE runtime_runs ADD COLUMN request_id TEXT NOT NULL DEFAULT ''"
                )
            if "runtime_state" not in columns:
                self._connection.execute(
                    "ALTER TABLE runtime_runs ADD COLUMN runtime_state TEXT NOT NULL DEFAULT 'CREATED'"
                )
            self._connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS runtime_runs_request_id_idx
                ON runtime_runs(tenant_id, request_id) WHERE request_id <> ''
                """
            )
            session_columns = {
                row["name"]
                for row in self._connection.execute("PRAGMA table_info(runtime_session_events)").fetchall()
            }
            for statement, column in [
                ("ALTER TABLE runtime_session_events ADD COLUMN parent_run_id TEXT NOT NULL DEFAULT ''", "parent_run_id"),
                ("ALTER TABLE runtime_session_events ADD COLUMN turn_id TEXT NOT NULL DEFAULT ''", "turn_id"),
                ("ALTER TABLE runtime_session_events ADD COLUMN step_id TEXT NOT NULL DEFAULT ''", "step_id"),
                ("ALTER TABLE runtime_session_events ADD COLUMN epoch_id TEXT NOT NULL DEFAULT ''", "epoch_id"),
                ("ALTER TABLE runtime_session_events ADD COLUMN attempt_id TEXT NOT NULL DEFAULT ''", "attempt_id"),
                ("ALTER TABLE runtime_session_events ADD COLUMN payload_version TEXT NOT NULL DEFAULT 'session-event/v1'", "payload_version"),
            ]:
                if column not in session_columns:
                    self._connection.execute(statement)
            outbox_columns = {
                row["name"]
                for row in self._connection.execute("PRAGMA table_info(runtime_outbox)").fetchall()
            }
            for statement, column in [
                (
                    "ALTER TABLE runtime_outbox ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0",
                    "attempts",
                ),
                ("ALTER TABLE runtime_outbox ADD COLUMN next_attempt_at TEXT", "next_attempt_at"),
                (
                    "ALTER TABLE runtime_outbox ADD COLUMN last_error TEXT NOT NULL DEFAULT ''",
                    "last_error",
                ),
            ]:
                if column not in outbox_columns:
                    self._connection.execute(statement)
            mailbox_columns = {
                row["name"]
                for row in self._connection.execute("PRAGMA table_info(runtime_run_mailbox)").fetchall()
            }
            if "priority" not in mailbox_columns:
                self._connection.execute(
                    "ALTER TABLE runtime_run_mailbox ADD COLUMN priority INTEGER NOT NULL DEFAULT 50"
                )
            self._connection.commit()

    def create(self, context: ExecutionContext) -> RuntimeRun:
        """以 ``tenant_id + request_id`` 幂等创建运行，重复请求返回原运行不重执行。"""
        run, _ = self.create_with_session_event(context)
        return run

    def create_with_session_event(
        self, context: ExecutionContext
    ) -> tuple[RuntimeRun, RuntimeLifecycleEvent | None]:
        """原子创建运行和 ``RUN_STARTED`` 事件；幂等重试不追加虚假的第二个开始事件。"""
        now = datetime.now(UTC)
        run = RuntimeRun(
            run_id=context.run_id,
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            agent_id=context.agent_id,
            snapshot_id=context.snapshot_id,
            status="RUNNING",
            runtime_state=AgentRunState.CREATED,
            context=context,
            created_at=now,
            updated_at=now,
        )
        with self._lock:
            existing = self._connection.execute(
                "SELECT * FROM runtime_runs WHERE tenant_id = ? AND request_id = ?",
                (context.tenant_id, context.request_id),
            ).fetchone()
            if existing:
                return self._from_row(existing), None
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                self._ensure_session_header_locked(context)
                self._connection.execute(
                """
                INSERT INTO runtime_runs (
                    run_id, tenant_id, user_id, agent_id, snapshot_id, request_id, status, runtime_state,
                    context_json, result_json, error_code, cancel_requested, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run.run_id,
                    run.tenant_id,
                    run.user_id,
                    run.agent_id,
                    run.snapshot_id,
                    context.request_id,
                    run.status,
                    run.runtime_state,
                    run.context.model_dump_json(),
                    "{}",
                    "",
                    0,
                    now.isoformat(),
                    now.isoformat(),
                ),
                )
                event = self._append_session_event_locked(
                    context,
                    RuntimeEventType.RUN_STARTED,
                    status="RUNNING",
                )
                self._refresh_projection_locked(context.tenant_id, context.session_id)
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise
        # SessionCreated 与 RunStarted 已原子落账；进程内总线仅通知本次 Run 的观察者。
        return run, event

    def get(self, tenant_id: str, run_id: str) -> RuntimeRun | None:
        """按租户读取运行，防止 run_id 碰撞或越权查询。"""
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM runtime_runs WHERE tenant_id = ? AND run_id = ?",
                (tenant_id, run_id),
            ).fetchone()
        return self._from_row(row) if row else None

    def is_run_ancestor(self, tenant_id: str, ancestor_run_id: str, descendant_run_id: str) -> bool:
        """沿持久化 ``parent_run_id`` 链验证谱系控制权，禁止并列 Agent 相互操控。"""
        if not ancestor_run_id or not descendant_run_id or ancestor_run_id == descendant_run_id:
            return False
        current = self.get(tenant_id, descendant_run_id)
        visited: set[str] = set()
        while current is not None and current.run_id not in visited:
            visited.add(current.run_id)
            parent_run_id = current.context.parent_run_id
            if parent_run_id == ancestor_run_id:
                return True
            if not parent_run_id:
                return False
            current = self.get(tenant_id, parent_run_id)
        return False

    def enqueue_mailbox_input(
        self,
        tenant_id: str,
        run_id: str,
        input_type: RunMailboxInputType | str,
        *,
        idempotency_key: str,
        priority: AgentInputPriority | int = AgentInputPriority.NORMAL,
    ) -> str:
        """登记不含正文的 Inbox 输入；同键重试不重复排队且优先级不能在重试时篡改。"""
        kind = RunMailboxInputType(input_type)
        requested_priority = AgentInputPriority(priority)
        key = idempotency_key.strip()
        if not key:
            raise ValueError("mailbox input requires an idempotency key")
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._connection.execute(
                    "SELECT runtime_state FROM runtime_runs WHERE tenant_id = ? AND run_id = ?",
                    (tenant_id, run_id),
                ).fetchone()
                if row is None:
                    raise LookupError("run not found")
                if AgentRunState(self._row_value(row, "runtime_state", "CREATED")).terminal:
                    raise InvalidRunTransition("terminal run rejects mailbox input")
                existing = self._connection.execute(
                    "SELECT message_id FROM runtime_run_mailbox "
                    "WHERE tenant_id = ? AND run_id = ? AND idempotency_key = ?",
                    (tenant_id, run_id, key),
                ).fetchone()
                if existing is not None:
                    self._connection.commit()
                    return str(existing["message_id"])
                message_id = f"mbx_{uuid4().hex}"
                self._connection.execute(
                    "INSERT INTO runtime_run_mailbox("
                    "message_id, tenant_id, run_id, input_type, idempotency_key, priority, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        message_id,
                        tenant_id,
                        run_id,
                        kind.value,
                        key,
                        int(requested_priority),
                        datetime.now(UTC).isoformat(),
                    ),
                )
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise
        return message_id

    def claim_mailbox_input(
        self, tenant_id: str, run_id: str
    ) -> ClaimedRunMailboxItem | None:
        """用短租约串行领取一条输入；崩溃未确认的项会在租约到期后重新可领。"""
        now = datetime.now(UTC)
        lease_token = f"lease_{uuid4().hex}"
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._connection.execute(
                    "SELECT * FROM runtime_run_mailbox WHERE tenant_id = ? AND run_id = ? "
                    "AND (delivery_status = 'PENDING' OR "
                    "(delivery_status = 'PROCESSING' AND lease_expires_at < ?)) "
                    "ORDER BY priority ASC, created_at ASC LIMIT 1",
                    (tenant_id, run_id, now.isoformat()),
                ).fetchone()
                if row is None:
                    self._connection.commit()
                    return None
                changed = self._connection.execute(
                    "UPDATE runtime_run_mailbox SET delivery_status = 'PROCESSING', lease_token = ?, "
                    "lease_expires_at = ? WHERE message_id = ? AND "
                    "(delivery_status = 'PENDING' OR (delivery_status = 'PROCESSING' AND lease_expires_at < ?))",
                    (
                        lease_token,
                        (now + timedelta(seconds=30)).isoformat(),
                        row["message_id"],
                        now.isoformat(),
                    ),
                )
                if not changed.rowcount:
                    self._connection.commit()
                    return None
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise
        return ClaimedRunMailboxItem(
            message_id=str(row["message_id"]),
            input_type=RunMailboxInputType(str(row["input_type"])),
            lease_token=lease_token,
            priority=AgentInputPriority(int(row["priority"])),
        )

    def has_pending_replan_input(self, tenant_id: str, run_id: str) -> bool:
        """只读检查未处理的计划变更输入，避免工具副作用抢在 Steering 之前提交。"""
        replan_types = tuple(
            item.value
            for item in (
                RunMailboxInputType.USER,
                RunMailboxInputType.STEERING,
                RunMailboxInputType.FOLLOW_UP,
                RunMailboxInputType.SYSTEM_CONTEXT,
            )
        )
        placeholders = ", ".join("?" for _ in replan_types)
        with self._lock:
            row = self._connection.execute(
                "SELECT 1 FROM runtime_run_mailbox WHERE tenant_id = ? AND run_id = ? "
                "AND delivery_status = 'PENDING' AND input_type IN ("
                f"{placeholders}) LIMIT 1",
                (tenant_id, run_id, *replan_types),
            ).fetchone()
        return row is not None

    def acknowledge_mailbox_input(self, message_id: str, lease_token: str) -> bool:
        """确认已由 Graph 重装 Context 的消息；无匹配租约不得误确认其他 Worker 的领取。"""
        with self._lock:
            changed = self._connection.execute(
                "UPDATE runtime_run_mailbox SET delivery_status = 'CONSUMED', consumed_at = ?, "
                "lease_token = '', lease_expires_at = NULL WHERE message_id = ? "
                "AND delivery_status = 'PROCESSING' AND lease_token = ?",
                (datetime.now(UTC).isoformat(), message_id, lease_token),
            )
            self._connection.commit()
        return bool(changed.rowcount)

    def transition_state(
        self,
        tenant_id: str,
        run_id: str,
        event: AgentRunEvent | str,
        *,
        metadata: dict[str, Any] | None = None,
        error_code: str = "",
    ) -> tuple[RuntimeRun | None, RuntimeLifecycleEvent | None]:
        """原子应用 Run 状态机事件并追加状态事实；非法迁移必须在副作用前失败关闭。"""
        trigger = AgentRunEvent(event)
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._connection.execute(
                    "SELECT * FROM runtime_runs WHERE tenant_id = ? AND run_id = ?",
                    (tenant_id, run_id),
                ).fetchone()
                if row is None:
                    self._connection.commit()
                    return None, None
                run = self._from_row(row)
                transition = transition_run_state(run.runtime_state, trigger)
                lifecycle_event = None
                if transition.changed:
                    now = datetime.now(UTC).isoformat()
                    self._connection.execute(
                        "UPDATE runtime_runs SET runtime_state = ?, updated_at = ? WHERE run_id = ?",
                        (transition.current.value, now, run_id),
                    )
                    lifecycle_event = self._append_session_event_locked(
                        run.context,
                        RuntimeEventType.RUN_STATE_CHANGED,
                        status=run.status,
                        error_code=error_code,
                        metadata={
                            "previous_state": transition.previous.value,
                            "current_state": transition.current.value,
                            "trigger": transition.event.value,
                            **(metadata or {}),
                        },
                    )
                    # 状态事实和治理 Outbox 同事务提交。Relay/CDC 之后才负责投递，
                    # 所以 Governance 故障不会反向中断主运行，也不会丢审计链。
                    self._enqueue_governance_locked(
                        _governance_event_for_state_change(run.context, lifecycle_event), now
                    )
                    self._refresh_projection_locked(tenant_id, run.context.session_id)
                    run = run.model_copy(update={"runtime_state": transition.current.value, "updated_at": datetime.fromisoformat(now)})
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise
        return run, lifecycle_event

    def cancel(self, tenant_id: str, run_id: str) -> RuntimeRun | None:
        """仅对活动/待审批运行写协作取消标记，终态不可被取消改写。"""
        run, _ = self.cancel_with_session_event(tenant_id, run_id)
        return run

    def cancel_with_session_event(
        self, tenant_id: str, run_id: str
    ) -> tuple[RuntimeRun | None, RuntimeLifecycleEvent | None]:
        """原子写入取消标记和取消事件；终态或重复取消不得制造额外事件。

        读取旧状态后以比较并交换方式更新，保证审计中的 ``previous_state`` 是真实
        先态而非更新后的取消态。等待用户输入同样属于活动 Run，必须允许取消。
        """
        now = datetime.now(UTC).isoformat()
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._connection.execute(
                    "SELECT * FROM runtime_runs WHERE tenant_id = ? AND run_id = ?",
                    (tenant_id, run_id),
                ).fetchone()
                previous_run = self._from_row(row) if row else None
                if previous_run is None:
                    self._connection.commit()
                    return None, None
                if previous_run.cancel_requested or AgentRunState(previous_run.runtime_state).terminal:
                    # 取消 API 必须幂等：重复调用只返回既有事实，不能因终态校验抛错。
                    self._connection.commit()
                    return previous_run, None
                transition_run_state(previous_run.runtime_state, AgentRunEvent.CANCEL_REQUESTED)
                changed = self._connection.execute(
                    """
                    UPDATE runtime_runs SET cancel_requested = 1, runtime_state = ?, updated_at = ?
                    WHERE tenant_id = ? AND run_id = ? AND cancel_requested = 0
                        AND runtime_state = ?
                        AND status IN ('RUNNING', 'WAITING_APPROVAL', 'WAITING_INPUT')
                    """,
                    (
                        AgentRunState.CANCELLED.value,
                        now,
                        tenant_id,
                        run_id,
                        previous_run.runtime_state,
                    ),
                )
                event = None
                if changed.rowcount:
                    event = self._append_session_event_locked(
                        previous_run.context,
                        RuntimeEventType.RUN_CANCEL_REQUESTED,
                        status=previous_run.status,
                        metadata={
                            "previous_state": previous_run.runtime_state,
                            "current_state": AgentRunState.CANCELLED.value,
                            "trigger": AgentRunEvent.CANCEL_REQUESTED.value,
                        },
                    )
                    self._enqueue_governance_locked(
                        _governance_event_for_state_change(previous_run.context, event), now
                    )
                    self._refresh_projection_locked(tenant_id, previous_run.context.session_id)
                    run = previous_run.model_copy(
                        update={"runtime_state": AgentRunState.CANCELLED.value, "cancel_requested": True}
                    )
                else:
                    run = self._from_row(
                        self._connection.execute(
                            "SELECT * FROM runtime_runs WHERE tenant_id = ? AND run_id = ?",
                            (tenant_id, run_id),
                        ).fetchone()
                    )
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise
        return run, event

    def finish(self, run_id: str, status: str, result: dict, error_code: str = "") -> None:
        """持久化运行结果；需治理事件时应优先用原子 ``finish_and_enqueue``。"""
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                runtime_state = self._next_state_for_status_locked(run_id, status)
                self._connection.execute(
                "UPDATE runtime_runs SET status = ?, runtime_state = ?, result_json = ?, error_code = ?, updated_at = ? WHERE run_id = ?",
                (
                    status,
                    runtime_state,
                    json.dumps(result, ensure_ascii=False),
                    error_code,
                    datetime.now(UTC).isoformat(),
                    run_id,
                ),
                )
                context = self._context_for_run_locked(run_id)
                if context is not None:
                    self._append_session_event_locked(
                        context, event_type_for_status(status), status=status, error_code=error_code
                    )
                    self._refresh_projection_locked(context.tenant_id, context.session_id)
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise

    def finish_and_enqueue(
        self,
        run_id: str,
        status: str,
        result: dict[str, Any],
        event: dict[str, Any],
        error_code: str = "",
    ) -> RuntimeLifecycleEvent | None:
        """原子持久化终态或中断态及对应治理事件，避免状态成功但审计事件丢失。"""
        now = datetime.now(UTC).isoformat()
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                previous_row = self._connection.execute(
                    "SELECT runtime_state FROM runtime_runs WHERE run_id = ?", (run_id,)
                ).fetchone()
                previous_state = (
                    self._row_value(previous_row, "runtime_state", AgentRunState.CREATED.value)
                    if previous_row is not None
                    else None
                )
                runtime_state = self._next_state_for_status_locked(run_id, status)
                self._connection.execute(
                    """
                    UPDATE runtime_runs
                    SET status = ?, runtime_state = ?, result_json = ?, error_code = ?, updated_at = ?
                    WHERE run_id = ?
                    """,
                    (
                        status,
                        runtime_state,
                        json.dumps(result, ensure_ascii=False),
                        error_code,
                        now,
                        run_id,
                    ),
                )
                self._enqueue_governance_locked(event, now)
                context = self._context_for_run_locked(run_id)
                state_event = None
                if context is not None and previous_state is not None and previous_state != runtime_state:
                    # 终态/中断态同样是状态迁移：保留专门的事实供跨 Run 审计关联，
                    # 而完成事件继续供 Governance 触发结果评测，二者职责不重叠。
                    state_event = self._append_session_event_locked(
                        context,
                        RuntimeEventType.RUN_STATE_CHANGED,
                        status=status,
                        error_code=error_code,
                        metadata={
                            "previous_state": previous_state,
                            "current_state": runtime_state,
                            "trigger": self._event_for_status(status).value,
                        },
                    )
                    self._enqueue_governance_locked(
                        _governance_event_for_state_change(context, state_event), now
                    )
                session_event = (
                    self._append_session_event_locked(
                        context,
                        event_type_for_status(status),
                        status=status,
                        error_code=error_code,
                        metadata={"steps": int(result.get("steps", 0))},
                    )
                    if context is not None
                    else None
                )
                if context is not None:
                    self._refresh_projection_locked(context.tenant_id, context.session_id)
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise
        return session_event

    def session_events(
        self, tenant_id: str, session_id: str, *, after_sequence: int = 0, limit: int = 200
    ) -> list[RuntimeLifecycleEvent]:
        """按租户、会话和序号读取追加日志，为回放提供稳定且不含敏感正文的事实流。"""
        if after_sequence < 0 or limit < 1 or limit > 1_000:
            raise ValueError("invalid session event pagination")
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT * FROM runtime_session_events
                WHERE tenant_id = ? AND session_id = ? AND sequence > ?
                ORDER BY sequence ASC LIMIT ?
                """,
                (tenant_id, session_id, after_sequence, limit),
            ).fetchall()
        return [self._session_event_from_row(row) for row in rows]

    def session_header(self, tenant_id: str, session_id: str) -> SessionHeader | None:
        """读取会话不可变锚点；Header 不由调用方提交，避免会话在运行中串版本。"""
        with self._lock:
            row = self._connection.execute(
                "SELECT header_json FROM runtime_sessions WHERE tenant_id = ? AND session_id = ?",
                (tenant_id, session_id),
            ).fetchone()
        return SessionHeader.model_validate_json(row["header_json"]) if row else None

    def session_projection(self, tenant_id: str, session_id: str) -> SessionProjection | None:
        """读取可再生的会话投影；不存在时从追加账本重放，不能把缓存误作事实源。"""
        header = self.session_header(tenant_id, session_id)
        if header is None:
            return None
        with self._lock:
            row = self._connection.execute(
                "SELECT projection_json FROM runtime_session_projections "
                "WHERE tenant_id = ? AND session_id = ?",
                (tenant_id, session_id),
            ).fetchone()
        if row is not None:
            return SessionProjection.model_validate_json(row["projection_json"])
        projection = derive_session_projection(
            header, self._all_session_events(tenant_id, session_id)
        )
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                self._save_projection_locked(projection)
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise
        return projection

    def session_model_surface(self, tenant_id: str, session_id: str) -> list[dict[str, str]]:
        """派生当前模型可见 Surface，Fork 只继承父会话指定前缀而不复制原始事件。"""
        return self._session_model_surface(tenant_id, session_id, set(), None)

    def _session_model_surface(
        self,
        tenant_id: str,
        session_id: str,
        visited: set[str],
        max_local_sequence: int | None,
    ) -> list[dict[str, str]]:
        """递归合并父前缀与本地替换 Surface；环形谱系直接拒绝，不能无限读取。"""
        if session_id in visited:
            raise ValueError("session fork lineage contains a cycle")
        header = self.session_header(tenant_id, session_id)
        if header is None:
            raise ValueError("session does not exist")
        lineage = {*visited, session_id}
        inherited: list[dict[str, str]] = []
        if header.parent_session_id:
            inherited = self._session_model_surface(
                tenant_id,
                header.parent_session_id,
                lineage,
                header.seed_sequence,
            )
        local_events = self._all_session_events(tenant_id, session_id)
        if max_local_sequence is not None:
            local_events = [
                event for event in local_events if event.sequence <= max_local_sequence
            ]
        return [*inherited, *derive_model_messages(local_events)]

    def _all_session_events(self, tenant_id: str, session_id: str) -> list[RuntimeLifecycleEvent]:
        """分页读取完整账本，防止 Surface、归档和回放被单页上限静默截断。"""
        events: list[RuntimeLifecycleEvent] = []
        after = 0
        while True:
            page = self.session_events(tenant_id, session_id, after_sequence=after, limit=1000)
            events.extend(page)
            if len(page) < 1000:
                return events
            after = page[-1].sequence

    def fork_session(
        self,
        *,
        tenant_id: str,
        source_session_id: str,
        new_session_id: str,
        owner_id: str,
        agent_id: str,
        agent_version: str,
        snapshot_id: str,
        seed_sequence: int | None = None,
        delegation_depth: int = 0,
    ) -> SessionHeader:
        """创建引用父会话前缀的子会话，不复制消息正文或篡改父会话事件。"""
        source = self.session_header(tenant_id, source_session_id)
        if source is None:
            raise ValueError("source session does not exist")
        if source.snapshot_id != snapshot_id:
            raise ValueError("forked session must keep the parent snapshot")
        projection = self.session_projection(tenant_id, source_session_id)
        maximum = projection.last_sequence if projection else 0
        sequence = maximum if seed_sequence is None else seed_sequence
        if sequence < 0 or sequence > maximum:
            raise ValueError("fork seed sequence is outside the parent session")
        header = SessionHeader(
            session_id=new_session_id,
            tenant_id=tenant_id,
            owner_id=owner_id,
            agent_id=agent_id,
            agent_version=agent_version,
            snapshot_id=snapshot_id,
            parent_session_id=source_session_id,
            seed_sequence=sequence,
            delegation_depth=delegation_depth,
            retention_class=source.retention_class,
            created_at=datetime.now(UTC),
        )
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                existing_row = self._connection.execute(
                    "SELECT header_json FROM runtime_sessions WHERE tenant_id = ? AND session_id = ?",
                    (tenant_id, new_session_id),
                ).fetchone()
                existing = (
                    SessionHeader.model_validate_json(existing_row["header_json"])
                    if existing_row is not None
                    else None
                )
                if existing is not None:
                    if (
                        existing.parent_session_id != header.parent_session_id
                        or existing.seed_sequence != header.seed_sequence
                        or existing.owner_id != header.owner_id
                        or existing.agent_id != header.agent_id
                        or existing.agent_version != header.agent_version
                        or existing.snapshot_id != header.snapshot_id
                    ):
                        raise ValueError("fork target session already exists with another header")
                    self._connection.commit()
                    return existing
                self._insert_session_header_locked(header)
                self._append_header_event_locked(header, RuntimeEventType.SESSION_FORKED, {
                    "parent_session_id": source_session_id,
                    "seed_sequence": sequence,
                })
                self._refresh_projection_locked(tenant_id, new_session_id)
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise
        return header

    def compact_session(
        self,
        tenant_id: str,
        session_id: str,
        *,
        replaced_through_sequence: int,
        summary: ModelVisibleMessage,
        policy_version: str,
    ) -> RuntimeLifecycleEvent:
        """追加压缩替换事件；旧事实保留，模型 Surface 以后只消费新的摘要投影。"""
        header = self.session_header(tenant_id, session_id)
        if header is None:
            raise ValueError("session does not exist")
        projection = self.session_projection(tenant_id, session_id)
        if projection is None or replaced_through_sequence <= 0:
            raise ValueError("session has no compactable events")
        if replaced_through_sequence > projection.last_sequence:
            raise ValueError("compaction sequence is outside the session")
        context = ExecutionContext(
            request_id=f"session-compaction-{uuid4().hex}",
            trace_id="",
            run_id="",
            session_id=session_id,
            parent_session_id=header.parent_session_id,
            tenant_id=tenant_id,
            user_id=header.owner_id,
            agent_id=header.agent_id,
            agent_version=header.agent_version,
            snapshot_id=header.snapshot_id,
            deadline_at=datetime.now(UTC),
            attempt_budget_remaining=0,
        )
        return self.append_session_event(
            context,
            RuntimeEventType.SESSION_COMPACTED,
            metadata={
                "replaced_through_sequence": replaced_through_sequence,
                "policy_version": policy_version,
            },
            model_message=summary,
        )

    def unresolved_tool_intents(
        self, tenant_id: str, session_id: str, run_id: str
    ) -> list[RuntimeLifecycleEvent]:
        """找出已落账但尚无结果事实的工具意图，供恢复前向 Gateway 对账。

        此方法只做语义关联，不根据“没有结果”擅自重试。是否已经执行由 Tool Gateway
        的幂等执行账本判定，避免 Runtime crash 后重复产生外部副作用。
        """
        events = self._all_session_events(tenant_id, session_id)
        completed = {
            str(event.metadata.get("tool_execution_id", ""))
            for event in events
            if event.run_id == run_id
            and event.event_type == RuntimeEventType.TOOL_RESULT
            and event.metadata.get("tool_execution_id")
        }
        return [
            event
            for event in events
            if event.run_id == run_id
            and event.event_type == RuntimeEventType.TOOL_INTENT_RECORDED
            and str(event.metadata.get("tool_execution_id", "")) not in completed
        ]

    def session_archive_payload(self, tenant_id: str, session_id: str) -> dict[str, Any]:
        """构造可写入对象存储的完整会话导出，调用方负责加密、WORM 与保留策略。"""
        header = self.session_header(tenant_id, session_id)
        if header is None:
            raise ValueError("session does not exist")
        events = self._all_session_events(tenant_id, session_id)
        projection = self.session_projection(tenant_id, session_id)
        return {
            "archive_contract_version": "session-archive/v1",
            "header": header.model_dump(mode="json"),
            "events": [event.model_dump(mode="json") for event in events],
            "projection": projection.model_dump(mode="json") if projection else None,
        }

    def record_session_archive(
        self,
        tenant_id: str,
        session_id: str,
        *,
        archive_key: str,
        archive_sha256: str,
        archived_through_sequence: int,
    ) -> None:
        """记录不可变归档定位信息；归档对象本体不回写到关系库，避免双份大正文。"""
        if archived_through_sequence < 1:
            raise ValueError("archived sequence must be positive")
        with self._lock:
            self._connection.execute(
                "INSERT INTO runtime_session_archives(tenant_id, session_id, archive_key, "
                "archive_sha256, archived_through_sequence, created_at) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(tenant_id, session_id, archived_through_sequence) DO NOTHING",
                (
                    tenant_id,
                    session_id,
                    archive_key,
                    archive_sha256,
                    archived_through_sequence,
                    datetime.now(UTC).isoformat(),
                ),
            )
            self._connection.commit()

    def latest_session_archive(self, tenant_id: str, session_id: str) -> dict[str, Any] | None:
        """返回最新归档的引用和校验摘要，不读取对象正文或绕过对象存储权限。"""
        with self._lock:
            row = self._connection.execute(
                "SELECT archive_key, archive_sha256, archived_through_sequence, created_at "
                "FROM runtime_session_archives WHERE tenant_id = ? AND session_id = ? "
                "ORDER BY archived_through_sequence DESC LIMIT 1",
                (tenant_id, session_id),
            ).fetchone()
        return dict(row) if row else None

    def append_session_event(
        self,
        context: ExecutionContext,
        event_type: RuntimeEventType,
        *,
        status: str = "RUNNING",
        metadata: dict[str, Any] | None = None,
        model_message: ModelVisibleMessage | None = None,
        turn_id: str = "",
        step_id: str = "",
        epoch_id: str = "",
        attempt_id: str = "",
    ) -> RuntimeLifecycleEvent:
        """独立追加已发生的步骤事实；每条事件仍以单事务获得会话单调序号。

        运行开始/结束事件与 Run 状态同事务写入；图内 Prompt、模型和工具事实在其副作用
        已完成后追加，不能被本地观察订阅者反向影响。
        """
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                event = self._append_session_event_locked(
                    context,
                    event_type,
                    status=status,
                    metadata=metadata,
                    model_message=model_message,
                    turn_id=turn_id,
                    step_id=step_id,
                    epoch_id=epoch_id,
                    attempt_id=attempt_id,
                )
                self._refresh_projection_locked(context.tenant_id, context.session_id)
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise
        return event

    def enqueue_governance(self, event: dict[str, Any]) -> None:
        """写入幂等治理 Outbox；业务事务内不直接同步调用 Kafka/HTTP。"""
        with self._lock:
            self._enqueue_governance_locked(event, datetime.now(UTC).isoformat())
            self._connection.commit()

    def _enqueue_governance_locked(self, event: dict[str, Any], created_at: str) -> None:
        """在既有事务中幂等插入治理事件，供状态和终态复用同一 Outbox 语义。"""
        self._connection.execute(
            "INSERT INTO runtime_outbox(event_id, payload_json, created_at) "
            "VALUES (?, ?, ?) ON CONFLICT(event_id) DO NOTHING",
            (event["event_id"], json.dumps(event, ensure_ascii=False), created_at),
        )

    def pending_events(self, limit: int = 100) -> list[dict[str, Any]]:
        """读取到期未投递事件，供 Relay 或直连发布器批量处理。"""
        now = datetime.now(UTC).isoformat()
        with self._lock:
            rows = self._connection.execute(
                "SELECT payload_json FROM runtime_outbox WHERE delivered_at IS NULL "
                "AND (next_attempt_at IS NULL OR next_attempt_at <= ?) "
                "ORDER BY created_at LIMIT ?",
                (now, limit),
            ).fetchall()
        return [json.loads(row["payload_json"]) for row in rows]

    def mark_delivered(self, event_id: str) -> None:
        """标记已投递；写入幂等以支持至少一次传输语义。"""
        with self._lock:
            self._connection.execute(
                "UPDATE runtime_outbox SET delivered_at = ? WHERE event_id = ?",
                (datetime.now(UTC).isoformat(), event_id),
            )
            self._connection.commit()

    def mark_delivery_failed(self, event_id: str, error: str) -> None:
        """记录 Outbox 投递失败并计算指数退避时间，不在业务事务中同步重试下游。"""
        with self._lock:
            row = self._connection.execute(
                "SELECT attempts FROM runtime_outbox WHERE event_id = ?", (event_id,)
            ).fetchone()
            attempts = int(row["attempts"]) + 1 if row else 1
            delay_seconds = min(900, 2 ** min(attempts, 9))
            self._connection.execute(
                "UPDATE runtime_outbox SET attempts = ?, next_attempt_at = ?, last_error = ? WHERE event_id = ?",
                (
                    attempts,
                    (datetime.now(UTC) + timedelta(seconds=delay_seconds)).isoformat(),
                    error[:1000],
                    event_id,
                ),
            )
            self._connection.commit()

    def close(self) -> None:
        """关闭开发 SQLite 连接；生产 PostgreSQL 适配器拥有独立生命周期。"""
        with self._lock:
            self._connection.close()

    def _context_for_run_locked(self, run_id: str) -> ExecutionContext | None:
        """在当前事务中读取运行上下文，避免 Session 事件跨事务丢失关联身份。"""
        row = self._connection.execute(
            "SELECT context_json FROM runtime_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        return ExecutionContext.model_validate_json(row["context_json"]) if row else None

    def _next_state_for_status_locked(self, run_id: str, status: str) -> str:
        """在写入 API 结果前校验终态迁移，防止迟到成功覆盖已取消或失败的 Run。"""
        row = self._connection.execute(
            "SELECT runtime_state FROM runtime_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            return self._terminal_state_for_status(status)
        transition = transition_run_state(
            self._row_value(row, "runtime_state", AgentRunState.CREATED.value),
            self._event_for_status(status),
        )
        return transition.current.value

    def _append_session_event_locked(
        self,
        context: ExecutionContext,
        event_type: RuntimeEventType,
        *,
        status: str,
        error_code: str = "",
        metadata: dict[str, Any] | None = None,
        model_message: ModelVisibleMessage | None = None,
        turn_id: str = "",
        step_id: str = "",
        epoch_id: str = "",
        attempt_id: str = "",
    ) -> RuntimeLifecycleEvent:
        """在调用方事务内分配会话序号并追加事件，禁止单独提交造成状态与回放分叉。"""
        self._lock_session_stream(context.tenant_id, context.session_id)
        row = self._connection.execute(
            """
            SELECT COALESCE(MAX(sequence), 0) AS last_sequence
            FROM runtime_session_events WHERE tenant_id = ? AND session_id = ?
            """,
            (context.tenant_id, context.session_id),
        ).fetchone()
        event = RuntimeLifecycleEvent.from_execution(
            context,
            event_type,
            sequence=int(row["last_sequence"]) + 1,
            status=status,
            error_code=error_code,
            metadata=metadata,
            model_message=model_message,
            turn_id=turn_id,
            step_id=step_id,
            epoch_id=epoch_id,
            attempt_id=attempt_id,
        )
        if self._schema_registry is not None:
            self._schema_registry.validate("session-event.v1.json", event.model_dump(mode="json"))
        self._connection.execute(
            """
            INSERT INTO runtime_session_events(
                event_id, tenant_id, session_id, run_id, parent_run_id, trace_id, agent_id, snapshot_id,
                sequence, event_type, status, error_code, turn_id, step_id, epoch_id, attempt_id,
                payload_version, metadata_json, occurred_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.event_id,
                event.tenant_id,
                event.session_id,
                event.run_id,
                event.parent_run_id,
                event.trace_id,
                event.agent_id,
                event.snapshot_id,
                event.sequence,
                event.event_type.value,
                event.status,
                event.error_code,
                event.turn_id,
                event.step_id,
                event.epoch_id,
                event.attempt_id,
                event.payload_version,
                json.dumps(
                    {
                        "metadata": event.metadata,
                        "model_message": (
                            event.model_message.model_dump(mode="json")
                            if event.model_message is not None
                            else None
                        ),
                    },
                    ensure_ascii=False,
                ),
                event.occurred_at.isoformat(),
            ),
        )
        return event

    def _lock_session_stream(self, tenant_id: str, session_id: str) -> None:
        """SQLite 已由进程内写锁串行化；PostgreSQL 适配器会覆盖为跨副本事务锁。"""
        del tenant_id, session_id

    def _ensure_session_header_locked(self, context: ExecutionContext) -> SessionHeader:
        """在创建首个 Run 的同一事务初始化会话锚点与 ``SessionCreated`` 事实。"""
        row = self._connection.execute(
            "SELECT header_json FROM runtime_sessions WHERE tenant_id = ? AND session_id = ?",
            (context.tenant_id, context.session_id),
        ).fetchone()
        if row is not None:
            header = SessionHeader.model_validate_json(row["header_json"])
            if (
                header.agent_id != context.agent_id
                or header.snapshot_id != context.snapshot_id
                or header.owner_id != context.user_id
            ):
                raise ValueError("session header does not match the executing release identity")
            return header
        header = SessionHeader(
            session_id=context.session_id,
            tenant_id=context.tenant_id,
            owner_id=context.user_id,
            agent_id=context.agent_id,
            agent_version=context.agent_version,
            snapshot_id=context.snapshot_id,
            parent_session_id=context.parent_session_id,
            delegation_depth=1 if context.parent_session_id else 0,
            created_at=datetime.now(UTC),
        )
        self._insert_session_header_locked(header)
        self._append_header_event_locked(header, RuntimeEventType.SESSION_CREATED, {})
        return header

    def _insert_session_header_locked(self, header: SessionHeader) -> None:
        """持久化不可变 Header；同名会话由数据库主键和事务锁共同保护。"""
        self._connection.execute(
            "INSERT INTO runtime_sessions(tenant_id, session_id, header_json, created_at) "
            "VALUES (?, ?, ?, ?)",
            (
                header.tenant_id,
                header.session_id,
                header.model_dump_json(),
                header.created_at.isoformat(),
            ),
        )

    def _append_header_event_locked(
        self, header: SessionHeader, event_type: RuntimeEventType, metadata: dict[str, Any]
    ) -> RuntimeLifecycleEvent:
        """为无 Run 的 Session 管理事件构造最小执行上下文并复用唯一追加路径。"""
        context = ExecutionContext(
            request_id=f"session-{header.session_id}",
            trace_id="",
            run_id="",
            session_id=header.session_id,
            parent_session_id=header.parent_session_id,
            tenant_id=header.tenant_id,
            user_id=header.owner_id,
            agent_id=header.agent_id,
            agent_version=header.agent_version,
            snapshot_id=header.snapshot_id,
            deadline_at=header.created_at,
            attempt_budget_remaining=0,
        )
        return self._append_session_event_locked(
            context, event_type, status="ACTIVE", metadata=metadata
        )

    def _refresh_projection_locked(self, tenant_id: str, session_id: str) -> None:
        """在事件提交前刷新物化投影，使读取视图与 Ledger 的已提交序号保持一致。"""
        header_row = self._connection.execute(
            "SELECT header_json FROM runtime_sessions WHERE tenant_id = ? AND session_id = ?",
            (tenant_id, session_id),
        ).fetchone()
        if header_row is None:
            return
        header = SessionHeader.model_validate_json(header_row["header_json"])
        rows = self._connection.execute(
            "SELECT * FROM runtime_session_events WHERE tenant_id = ? AND session_id = ? "
            "ORDER BY sequence ASC",
            (tenant_id, session_id),
        ).fetchall()
        projection = derive_session_projection(
            header, [self._session_event_from_row(row) for row in rows]
        )
        self._save_projection_locked(projection)

    def _save_projection_locked(self, projection: SessionProjection) -> None:
        """以幂等替换保存派生投影；原始 Event Ledger 从不被该操作修改。"""
        self._connection.execute(
            "INSERT INTO runtime_session_projections(tenant_id, session_id, projection_json, updated_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(tenant_id, session_id) DO UPDATE SET "
            "projection_json = excluded.projection_json, updated_at = excluded.updated_at",
            (
                projection.tenant_id,
                projection.session_id,
                projection.model_dump_json(),
                projection.updated_at.isoformat(),
            ),
        )

    @staticmethod
    def _session_event_from_row(row: sqlite3.Row) -> RuntimeLifecycleEvent:
        """恢复持久化事件的严格序号和类型，拒绝把裸 SQL 行泄漏给回放调用方。"""
        return RuntimeLifecycleEvent(
            event_id=row["event_id"],
            sequence=int(row["sequence"]),
            event_type=RuntimeEventType(row["event_type"]),
            occurred_at=datetime.fromisoformat(row["occurred_at"]),
            run_id=row["run_id"],
            parent_run_id=row["parent_run_id"],
            trace_id=row["trace_id"],
            tenant_id=row["tenant_id"],
            agent_id=row["agent_id"],
            snapshot_id=row["snapshot_id"],
            session_id=row["session_id"],
            turn_id=RuntimeStore._row_value(row, "turn_id", ""),
            step_id=RuntimeStore._row_value(row, "step_id", ""),
            epoch_id=RuntimeStore._row_value(row, "epoch_id", ""),
            attempt_id=RuntimeStore._row_value(row, "attempt_id", ""),
            payload_version=RuntimeStore._row_value(
                row, "payload_version", "session-event/v1"
            ),
            status=row["status"],
            error_code=row["error_code"],
            metadata=RuntimeStore._event_payload(row["metadata_json"])[0],
            model_message=RuntimeStore._event_payload(row["metadata_json"])[1],
        )

    @staticmethod
    def _event_payload(value: str) -> tuple[dict[str, Any], ModelVisibleMessage | None]:
        """兼容旧版纯 metadata JSON，并恢复新版受限模型消息投影。"""
        decoded = json.loads(value)
        if not isinstance(decoded, dict) or "metadata" not in decoded:
            return (decoded if isinstance(decoded, dict) else {}, None)
        message = decoded.get("model_message")
        return (
            dict(decoded.get("metadata") or {}),
            ModelVisibleMessage.model_validate(message) if message else None,
        )

    @staticmethod
    def _row_value(row: sqlite3.Row, key: str, default: str) -> str:
        """兼容历史迁移期间缺少的新列；SQLite Row 没有 dict.get 语义。"""
        columns = set(row.keys())
        return str(row[key]) if key in columns else default

    @staticmethod
    def _from_row(row: sqlite3.Row) -> RuntimeRun:
        """反序列化持久化行并恢复强类型上下文，避免裸字典进入执行层。"""
        return RuntimeRun(
            run_id=row["run_id"],
            tenant_id=row["tenant_id"],
            user_id=row["user_id"],
            agent_id=row["agent_id"],
            snapshot_id=row["snapshot_id"],
            status=row["status"],
            runtime_state=RuntimeStore._row_value(row, "runtime_state", AgentRunState.CREATED.value),
            context=ExecutionContext.model_validate_json(row["context_json"]),
            result=json.loads(row["result_json"]),
            error_code=row["error_code"],
            cancel_requested=bool(row["cancel_requested"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    @staticmethod
    def _terminal_state_for_status(status: str) -> str:
        """把粗粒度 API 状态映射为规范 Run 状态，保持旧对外状态字段兼容。"""
        if status == "WAITING_APPROVAL":
            return AgentRunState.WAITING_APPROVAL.value
        if status == "WAITING_INPUT":
            return AgentRunState.WAITING_INPUT.value
        if status == "CANCELLED":
            return AgentRunState.CANCELLED.value
        if status == "COMPLETED":
            return AgentRunState.COMPLETED.value
        return AgentRunState.FAILED.value

    @staticmethod
    def _event_for_status(status: str) -> AgentRunEvent:
        """将旧 API 状态映射为状态机事件，保证终态也经过同一合法迁移校验。"""
        if status == "WAITING_APPROVAL":
            return AgentRunEvent.APPROVAL_REQUIRED
        if status == "WAITING_INPUT":
            return AgentRunEvent.INPUT_REQUIRED
        if status == "CANCELLED":
            return AgentRunEvent.CANCEL_REQUESTED
        if status == "COMPLETED":
            return AgentRunEvent.RUN_COMPLETED
        return AgentRunEvent.RUN_FAILED


class RuntimeStore(RuntimeStoreOperations):
    """本地 SQLite 开发存储；生产 PostgreSQL 适配器不继承该具体后端。"""


class GovernanceOutboxPublisher:
    def __init__(
        self,
        store: RuntimeStore,
        base_url: str,
        event_key: str,
        timeout: float,
        workload_identity: WorkloadTokenProvider | None = None,
        delivery_mode: str = "direct",
        mtls: dict[str, Any] | None = None,
    ) -> None:
        """配置 Outbox 交付器；CDC 模式不发 HTTP，避免 Connect 与直连双投递。"""
        self.store, self.base_url, self.event_key, self.timeout = (
            store,
            base_url.rstrip("/"),
            event_key,
            timeout,
        )
        self.workload_identity = workload_identity
        self.delivery_mode = delivery_mode
        self.mtls = mtls or {}

    def publish_run(
        self,
        context: ExecutionContext,
        status: str,
        result: dict[str, Any],
        error_code: str = "",
    ) -> None:
        """为运行状态生成治理事件并先写入 Outbox，不阻塞主业务流。"""
        self.store.enqueue_governance(self.event_for_run(context, status, result, error_code))

    @staticmethod
    def event_for_run(
        context: ExecutionContext,
        status: str,
        result: dict[str, Any],
        error_code: str = "",
    ) -> dict[str, Any]:
        """构造关联快照、路由、证据与成本摘要的治理事件。"""
        plan = result.get("execution_plan", {})
        complexity = plan.get("complexity", {}) if isinstance(plan, dict) else {}
        route = plan.get("route", {}) if isinstance(plan, dict) else {}
        budget = result.get("budget", {})
        event_type = (
            "agent.run.interrupted"
            if status in {"WAITING_APPROVAL", "WAITING_INPUT"}
            else "agent.run.completed"
        )
        return {
            "event_id": f"evt_{uuid4().hex}",
            "source_service": "agent-runtime",
            "event_type": event_type,
            "trace_id": context.trace_id,
            "tenant_id": context.tenant_id,
            "occurred_at": datetime.now(UTC).isoformat(),
            "payload": {
                "run_id": context.run_id,
                "agent_id": context.agent_id,
                "snapshot_id": context.snapshot_id,
                "agent_version": context.agent_version,
                "status": status,
                "error_code": error_code,
                "graph_version": context.graph_version,
                "steps": result.get("steps", 0),
                "evidence_count": len(result.get("evidence", [])),
                "intent": plan.get("intent", {}).get("name") if isinstance(plan, dict) else None,
                "route": route.get("route"),
                "complexity_score": complexity.get("score"),
                "execution_plan_id": plan.get("plan_id") if isinstance(plan, dict) else None,
                "execution_plan_hash": plan.get("plan_hash") if isinstance(plan, dict) else None,
                "planner_version": plan.get("planner_version") if isinstance(plan, dict) else None,
                "analyzer_version": plan.get("analyzer_version") if isinstance(plan, dict) else None,
                "input_fingerprint": plan.get("input_fingerprint") if isinstance(plan, dict) else None,
                "policy_fingerprint": plan.get("policy_fingerprint") if isinstance(plan, dict) else None,
                "cost_usd": budget.get("spent_cost_usd", 0),
                "latency_ms": result.get("latency_ms", 0),
            },
        }

    def flush(self) -> None:
        """direct 模式重试 HTTP 投递；CDC 模式保留事件供 Connect 独占读取。"""
        # In CDC mode Kafka Connect is the sole transport owner.  Keeping the
        # row untouched preserves an immutable audit source and avoids HTTP/
        # CDC double delivery from the same completed runtime transaction.
        if self.delivery_mode == "cdc" or not self.base_url:
            return
        headers = {"X-Governance-Event-Key": self.event_key} if self.event_key else {}
        if self.workload_identity is not None:
            headers.update(self.workload_identity.authorization_header())
        for event in self.store.pending_events():
            try:
                response = httpx.post(
                    f"{self.base_url}/v1/governance/events",
                    json=event,
                    headers=headers,
                    timeout=self.timeout,
                    **self.mtls,
                )
                response.raise_for_status()
                self.store.mark_delivered(event["event_id"])
            except httpx.HTTPError as exc:
                self.store.mark_delivery_failed(event["event_id"], str(exc))
