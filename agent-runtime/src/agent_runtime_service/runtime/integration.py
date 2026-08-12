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
from platform_sdk.contracts.execution import ExecutionContext, RuntimeRun


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


class RuntimeStore:
    """Small durable Run + transactional-outbox store; PostgreSQL is the production adapter."""

    def __init__(self, path: Path) -> None:
        """初始化本地 Run 与 Transactional Outbox 存储，仅作 PostgreSQL 的开发替身。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._lock = Lock()
        with self._lock:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS runtime_runs (
                    run_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, user_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL, snapshot_id TEXT NOT NULL, request_id TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    context_json TEXT NOT NULL, result_json TEXT NOT NULL, error_code TEXT NOT NULL,
                    cancel_requested INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS runtime_outbox (
                    event_id TEXT PRIMARY KEY, payload_json TEXT NOT NULL, delivered_at TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at TEXT,
                    last_error TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
                );
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
            self._connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS runtime_runs_request_id_idx
                ON runtime_runs(tenant_id, request_id) WHERE request_id <> ''
                """
            )
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
            self._connection.commit()

    def create(self, context: ExecutionContext) -> RuntimeRun:
        """以 ``tenant_id + request_id`` 幂等创建运行，重复请求返回原运行不重执行。"""
        now = datetime.now(UTC)
        run = RuntimeRun(
            run_id=context.run_id,
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            agent_id=context.agent_id,
            snapshot_id=context.snapshot_id,
            status="RUNNING",
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
                return self._from_row(existing)
            self._connection.execute(
                """
                INSERT INTO runtime_runs (
                    run_id, tenant_id, user_id, agent_id, snapshot_id, request_id, status,
                    context_json, result_json, error_code, cancel_requested, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run.run_id,
                    run.tenant_id,
                    run.user_id,
                    run.agent_id,
                    run.snapshot_id,
                    context.request_id,
                    run.status,
                    run.context.model_dump_json(),
                    "{}",
                    "",
                    0,
                    now.isoformat(),
                    now.isoformat(),
                ),
            )
            self._connection.commit()
        return run

    def get(self, tenant_id: str, run_id: str) -> RuntimeRun | None:
        """按租户读取运行，防止 run_id 碰撞或越权查询。"""
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM runtime_runs WHERE tenant_id = ? AND run_id = ?",
                (tenant_id, run_id),
            ).fetchone()
        return self._from_row(row) if row else None

    def cancel(self, tenant_id: str, run_id: str) -> RuntimeRun | None:
        """仅对活动/待审批运行写协作取消标记，终态不可被取消改写。"""
        now = datetime.now(UTC).isoformat()
        with self._lock:
            self._connection.execute(
                """
                UPDATE runtime_runs SET cancel_requested = 1, updated_at = ?
                WHERE tenant_id = ? AND run_id = ? AND status IN ('RUNNING', 'WAITING_APPROVAL')
                """,
                (now, tenant_id, run_id),
            )
            self._connection.commit()
        return self.get(tenant_id, run_id)

    def finish(self, run_id: str, status: str, result: dict, error_code: str = "") -> None:
        """持久化运行结果；需治理事件时应优先用原子 ``finish_and_enqueue``。"""
        with self._lock:
            self._connection.execute(
                "UPDATE runtime_runs SET status = ?, result_json = ?, error_code = ?, updated_at = ? WHERE run_id = ?",
                (
                    status,
                    json.dumps(result, ensure_ascii=False),
                    error_code,
                    datetime.now(UTC).isoformat(),
                    run_id,
                ),
            )
            self._connection.commit()

    def finish_and_enqueue(
        self,
        run_id: str,
        status: str,
        result: dict[str, Any],
        event: dict[str, Any],
        error_code: str = "",
    ) -> None:
        """原子持久化终态或中断态及对应治理事件，避免状态成功但审计事件丢失。"""
        now = datetime.now(UTC).isoformat()
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                self._connection.execute(
                    """
                    UPDATE runtime_runs
                    SET status = ?, result_json = ?, error_code = ?, updated_at = ?
                    WHERE run_id = ?
                    """,
                    (
                        status,
                        json.dumps(result, ensure_ascii=False),
                        error_code,
                        now,
                        run_id,
                    ),
                )
                self._connection.execute(
                    """
                    INSERT INTO runtime_outbox(event_id, payload_json, created_at)
                    VALUES (?, ?, ?) ON CONFLICT(event_id) DO NOTHING
                    """,
                    (event["event_id"], json.dumps(event, ensure_ascii=False), now),
                )
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise

    def enqueue_governance(self, event: dict[str, Any]) -> None:
        """写入幂等治理 Outbox；业务事务内不直接同步调用 Kafka/HTTP。"""
        with self._lock:
            self._connection.execute(
                "INSERT INTO runtime_outbox(event_id, payload_json, created_at) "
                "VALUES (?, ?, ?) ON CONFLICT(event_id) DO NOTHING",
                (
                    event["event_id"],
                    json.dumps(event, ensure_ascii=False),
                    datetime.now(UTC).isoformat(),
                ),
            )
            self._connection.commit()

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
            context=ExecutionContext.model_validate_json(row["context_json"]),
            result=json.loads(row["result_json"]),
            error_code=row["error_code"],
            cancel_requested=bool(row["cancel_requested"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )


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
            "agent.run.interrupted" if status == "WAITING_APPROVAL" else "agent.run.completed"
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
