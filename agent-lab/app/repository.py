"""Agent Lab 持久化边界：本地 SQLite 与生产 PostgreSQL 使用同一受控契约。"""

from __future__ import annotations

import re
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Lock
from typing import Protocol

from platform_infra.postgres import connect_postgres

from app.models import ExperimentJob, ExperimentJobStatus, ExperimentRecord


class ExperimentRepositoryPort(Protocol):
    """定义实验聚合和调度任务的最小存储契约，避免应用层依赖某个数据库驱动。"""

    def create(self, record: ExperimentRecord) -> ExperimentRecord:
        """原子创建实验聚合，重复实验标识必须显式失败。"""
        ...

    def get(self, tenant_id: str, experiment_id: str) -> ExperimentRecord | None:
        """按租户读取实验聚合，禁止跨租户暴露回放内容。"""
        ...

    def save(self, record: ExperimentRecord) -> ExperimentRecord:
        """保存已完成合法状态迁移的完整实验聚合。"""
        ...

    def enqueue(self, job: ExperimentJob) -> ExperimentJob:
        """持久化唯一待执行任务，调度系统不得绕过该写入边界。"""
        ...

    def get_job(self, tenant_id: str, job_id: str) -> ExperimentJob | None:
        """按租户查询任务，供 API 返回安全的调度状态。"""
        ...

    def get_job_for_worker(self, job_id: str) -> ExperimentJob | None:
        """允许受隔离 Worker 按任务标识读取执行对象。"""
        ...

    def get_job_for_experiment(
        self, tenant_id: str, experiment_id: str
    ) -> ExperimentJob | None:
        """读取实验唯一任务，以便 API 幂等恢复调度提交。"""
        ...

    def claim(self, job_id: str, worker_id: str, lease_seconds: int) -> ExperimentJob | None:
        """以租约领取可执行任务，崩溃后允许超时恢复。"""
        ...

    def complete(self, job_id: str, worker_id: str) -> ExperimentJob:
        """由当前租约持有者结算成功任务。"""
        ...

    def retry_or_dead_letter(
        self, job_id: str, worker_id: str, error: str, retry_delay_seconds: int
    ) -> ExperimentJob:
        """保存失败原因，并在重试预算耗尽后转入 DLQ。"""
        ...

    def close(self) -> None:
        """释放 Repository 所拥有的本地基础设施资源。"""
        ...


class ExperimentRepository:
    """SQLite 本地适配器：只为开发与契约测试提供单进程持久化和租约语义。"""

    def __init__(self, path: Path) -> None:
        """创建本地表和进程内写锁；生产配置不得选择此实现。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._lock = Lock()
        with self._lock:
            self._connection.executescript(_SQLITE_SCHEMA)
            self._connection.commit()

    def create(self, record: ExperimentRecord) -> ExperimentRecord:
        """原子保存新实验；重复标识符失败，不能覆盖已冻结的实验输入。"""
        with self._lock:
            self._connection.execute(
                "INSERT INTO agent_lab_experiments VALUES (?, ?, ?, ?, ?)",
                _record_values(record),
            )
            self._connection.commit()
        return record

    def get(self, tenant_id: str, experiment_id: str) -> ExperimentRecord | None:
        """按租户读取实验聚合，阻止跨租户获取离线 Trace 与评测结果。"""
        with self._lock:
            row = self._connection.execute(
                "SELECT payload_json FROM agent_lab_experiments "
                "WHERE experiment_id = ? AND tenant_id = ?",
                (experiment_id, tenant_id),
            ).fetchone()
        return ExperimentRecord.model_validate_json(row["payload_json"]) if row else None

    def save(self, record: ExperimentRecord) -> ExperimentRecord:
        """替换完整聚合；调用方必须在写入前完成合法业务状态迁移。"""
        with self._lock:
            cursor = self._connection.execute(
                """
                UPDATE agent_lab_experiments
                SET status = ?, payload_json = ?, updated_at = ?
                WHERE experiment_id = ? AND tenant_id = ?
                """,
                _record_values(record)[2:] + _record_values(record)[:2],
            )
            if cursor.rowcount != 1:
                raise KeyError(record.experiment_id)
            self._connection.commit()
        return record

    def enqueue(self, job: ExperimentJob) -> ExperimentJob:
        """记录待执行任务；每个实验只有一个任务，避免重复提交并行回放同一快照。"""
        with self._lock:
            self._connection.execute(
                """
                INSERT INTO agent_lab_jobs (
                    job_id, experiment_id, tenant_id, status, attempt_count, max_attempts,
                    available_at, leased_by, lease_expires_at, last_error, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                _job_values(job),
            )
            self._connection.commit()
        return job

    def get_job(self, tenant_id: str, job_id: str) -> ExperimentJob | None:
        """按租户读取任务状态，供 API 查询调度进度而不暴露其他租户任务。"""
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM agent_lab_jobs WHERE job_id = ? AND tenant_id = ?",
                (job_id, tenant_id),
            ).fetchone()
        return _job_from_row(row) if row else None

    def get_job_for_worker(self, job_id: str) -> ExperimentJob | None:
        """供受网络隔离的 Worker 读取任务；租户授权始终由 API 层负责而非由 Worker 猜测。"""
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM agent_lab_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        return _job_from_row(row) if row else None

    def get_job_for_experiment(self, tenant_id: str, experiment_id: str) -> ExperimentJob | None:
        """读取实验对应的唯一任务，使 API 在 Temporal 短暂不可达后能幂等重新提交。"""
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM agent_lab_jobs WHERE experiment_id = ? AND tenant_id = ?",
                (experiment_id, tenant_id),
            ).fetchone()
        return _job_from_row(row) if row else None

    def claim(self, job_id: str, worker_id: str, lease_seconds: int) -> ExperimentJob | None:
        """用短事务领取到期任务；崩溃 Worker 的过期租约会由下一次领取回收。"""
        now = _now()
        expires = now + timedelta(seconds=lease_seconds)
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                self._connection.execute(
                    """
                    UPDATE agent_lab_jobs
                    SET status = ?, leased_by = NULL, lease_expires_at = NULL, updated_at = ?
                    WHERE job_id = ? AND status = ? AND lease_expires_at < ?
                    """,
                    (
                        ExperimentJobStatus.RETRY_SCHEDULED.value,
                        _iso(now),
                        job_id,
                        ExperimentJobStatus.RUNNING.value,
                        _iso(now),
                    ),
                )
                row = self._connection.execute(
                    """
                    SELECT * FROM agent_lab_jobs
                    WHERE job_id = ? AND status IN (?, ?) AND available_at <= ?
                    """,
                    (
                        job_id,
                        ExperimentJobStatus.QUEUED.value,
                        ExperimentJobStatus.RETRY_SCHEDULED.value,
                        _iso(now),
                    ),
                ).fetchone()
                if row is None:
                    self._connection.commit()
                    return None
                self._connection.execute(
                    """
                    UPDATE agent_lab_jobs
                    SET status = ?, attempt_count = attempt_count + 1, leased_by = ?,
                        lease_expires_at = ?, updated_at = ?
                    WHERE job_id = ?
                    """,
                    (
                        ExperimentJobStatus.RUNNING.value,
                        worker_id,
                        _iso(expires),
                        _iso(now),
                        job_id,
                    ),
                )
                claimed = self._connection.execute(
                    "SELECT * FROM agent_lab_jobs WHERE job_id = ?", (job_id,)
                ).fetchone()
                self._connection.commit()
                return _job_from_row(claimed)
            except Exception:
                self._connection.rollback()
                raise

    def complete(self, job_id: str, worker_id: str) -> ExperimentJob:
        """完成当前 Worker 租约持有的任务；租约失效时拒绝旧 Worker 覆盖新结果。"""
        return self._finish(job_id, worker_id, status=ExperimentJobStatus.COMPLETED)

    def retry_or_dead_letter(
        self, job_id: str, worker_id: str, error: str, retry_delay_seconds: int
    ) -> ExperimentJob:
        """按尝试上限转入 DLQ 或按指数退避时间重新排队，并保留截断后的故障原因。"""
        now = _now()
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM agent_lab_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            _assert_lease(row, worker_id, job_id)
            current = _job_from_row(row)
            status = (
                ExperimentJobStatus.DLQ
                if current.attempt_count >= current.max_attempts
                else ExperimentJobStatus.RETRY_SCHEDULED
            )
            available = now + timedelta(seconds=retry_delay_seconds)
            self._connection.execute(
                """
                UPDATE agent_lab_jobs
                SET status = ?, available_at = ?, leased_by = NULL, lease_expires_at = NULL,
                    last_error = ?, updated_at = ?
                WHERE job_id = ? AND leased_by = ? AND status = ?
                """,
                (
                    status.value,
                    _iso(available),
                    error[:4000],
                    _iso(now),
                    job_id,
                    worker_id,
                    ExperimentJobStatus.RUNNING.value,
                ),
            )
            self._connection.commit()
            updated = self._connection.execute(
                "SELECT * FROM agent_lab_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            return _job_from_row(updated) if updated else current

    def _finish(
        self, job_id: str, worker_id: str, *, status: ExperimentJobStatus
    ) -> ExperimentJob:
        """在持有租约时写入终态，防止失联 Worker 与恢复 Worker 竞态提交。"""
        now = _now()
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM agent_lab_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            _assert_lease(row, worker_id, job_id)
            current = _job_from_row(row)
            self._connection.execute(
                """
                UPDATE agent_lab_jobs
                SET status = ?, leased_by = NULL, lease_expires_at = NULL, updated_at = ?
                WHERE job_id = ? AND leased_by = ? AND status = ?
                """,
                (status.value, _iso(now), job_id, worker_id, ExperimentJobStatus.RUNNING.value),
            )
            self._connection.commit()
            updated = self._connection.execute(
                "SELECT * FROM agent_lab_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            return _job_from_row(updated) if updated else current

    def close(self) -> None:
        """关闭本地连接，供应用生命周期安全释放开发期资源。"""
        self._connection.close()


class PostgresExperimentRepository:
    """PostgreSQL 生产适配器：每次操作使用短事务，并以行锁实现跨副本租约。"""

    def __init__(self, dsn: str, schema: str = "agent_lab") -> None:
        """保存受限 Schema 的连接信息；不继承 SQLite 适配器或复用其事务语义。"""
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", schema):
            raise ValueError("invalid PostgreSQL schema name")
        self._dsn = dsn
        self._schema = schema

    def initialize(self) -> None:
        """在接收 API 或 Worker 流量前创建独立 Schema、聚合表及任务队列表。"""
        with connect_postgres(self._dsn, "public") as connection:
            connection.execute(f'CREATE SCHEMA IF NOT EXISTS "{self._schema}"')
            connection.commit()
        with self._connect() as connection:
            for statement in _POSTGRES_SCHEMA:
                connection.execute(statement)
            connection.commit()

    def create(self, record: ExperimentRecord) -> ExperimentRecord:
        """在 PostgreSQL 中写入冻结实验聚合，冲突保留给调用方显式处理。"""
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO agent_lab_experiments VALUES (?, ?, ?, ?, ?)",
                _record_values(record),
            )
            connection.commit()
        return record

    def get(self, tenant_id: str, experiment_id: str) -> ExperimentRecord | None:
        """在数据库过滤租户与实验标识，避免应用层过滤导致跨租户泄漏。"""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM agent_lab_experiments "
                "WHERE experiment_id = ? AND tenant_id = ?",
                (experiment_id, tenant_id),
            ).fetchone()
        return ExperimentRecord.model_validate_json(row["payload_json"]) if row else None

    def save(self, record: ExperimentRecord) -> ExperimentRecord:
        """在单个 PostgreSQL 事务中更新聚合；找不到行时返回与本地相同的 KeyError。"""
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE agent_lab_experiments
                SET status = ?, payload_json = ?, updated_at = ?
                WHERE experiment_id = ? AND tenant_id = ?
                """,
                _record_values(record)[2:] + _record_values(record)[:2],
            )
            if cursor.rowcount != 1:
                connection.rollback()
                raise KeyError(record.experiment_id)
            connection.commit()
        return record

    def enqueue(self, job: ExperimentJob) -> ExperimentJob:
        """持久化 Temporal 工作流对应的任务键；唯一实验约束阻止重复执行。"""
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO agent_lab_jobs (
                    job_id, experiment_id, tenant_id, status, attempt_count, max_attempts,
                    available_at, leased_by, lease_expires_at, last_error, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                _job_values(job),
            )
            connection.commit()
        return job

    def get_job(self, tenant_id: str, job_id: str) -> ExperimentJob | None:
        """读取同一租户的调度任务，不允许 Worker 或 API 跨租户猜测任务状态。"""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM agent_lab_jobs WHERE job_id = ? AND tenant_id = ?",
                (job_id, tenant_id),
            ).fetchone()
        return _job_from_row(row) if row else None

    def get_job_for_worker(self, job_id: str) -> ExperimentJob | None:
        """供同一受信任 Worker 部署读取任务；外部读取必须使用带 tenant 的方法。"""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM agent_lab_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        return _job_from_row(row) if row else None

    def get_job_for_experiment(self, tenant_id: str, experiment_id: str) -> ExperimentJob | None:
        """按租户读取实验唯一任务，用于安全恢复 Temporal 提交而非扫描其他租户队列。"""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM agent_lab_jobs WHERE experiment_id = ? AND tenant_id = ?",
                (experiment_id, tenant_id),
            ).fetchone()
        return _job_from_row(row) if row else None

    def claim(self, job_id: str, worker_id: str, lease_seconds: int) -> ExperimentJob | None:
        """通过 FOR UPDATE SKIP LOCKED 领取任务，允许多个 Worker 无阻塞地竞争不同实验。"""
        now = _now()
        expires = now + timedelta(seconds=lease_seconds)
        with self._connect() as connection:
            try:
                connection.execute(
                    """
                    UPDATE agent_lab_jobs
                    SET status = ?, leased_by = NULL, lease_expires_at = NULL, updated_at = ?
                    WHERE job_id = ? AND status = ? AND lease_expires_at < ?
                    """,
                    (
                        ExperimentJobStatus.RETRY_SCHEDULED.value,
                        _iso(now),
                        job_id,
                        ExperimentJobStatus.RUNNING.value,
                        _iso(now),
                    ),
                )
                row = connection.execute(
                    """
                    SELECT * FROM agent_lab_jobs
                    WHERE job_id = ? AND status IN (?, ?) AND available_at <= ?
                    FOR UPDATE SKIP LOCKED
                    """,
                    (
                        job_id,
                        ExperimentJobStatus.QUEUED.value,
                        ExperimentJobStatus.RETRY_SCHEDULED.value,
                        _iso(now),
                    ),
                ).fetchone()
                if row is None:
                    connection.commit()
                    return None
                connection.execute(
                    """
                    UPDATE agent_lab_jobs
                    SET status = ?, attempt_count = attempt_count + 1, leased_by = ?,
                        lease_expires_at = ?, updated_at = ?
                    WHERE job_id = ?
                    """,
                    (
                        ExperimentJobStatus.RUNNING.value,
                        worker_id,
                        _iso(expires),
                        _iso(now),
                        job_id,
                    ),
                )
                claimed = connection.execute(
                    "SELECT * FROM agent_lab_jobs WHERE job_id = ?", (job_id,)
                ).fetchone()
                connection.commit()
                return _job_from_row(claimed)
            except Exception:
                connection.rollback()
                raise

    def complete(self, job_id: str, worker_id: str) -> ExperimentJob:
        """仅允许当前租约持有者把任务标记完成，阻断过期 Worker 的迟到写入。"""
        return self._finish(job_id, worker_id, status=ExperimentJobStatus.COMPLETED)

    def retry_or_dead_letter(
        self, job_id: str, worker_id: str, error: str, retry_delay_seconds: int
    ) -> ExperimentJob:
        """保留失败原因并按最大尝试次数决定重试或进入持久化 DLQ。"""
        now = _now()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM agent_lab_jobs WHERE job_id = ? FOR UPDATE", (job_id,)
            ).fetchone()
            _assert_lease(row, worker_id, job_id)
            current = _job_from_row(row)
            status = (
                ExperimentJobStatus.DLQ
                if current.attempt_count >= current.max_attempts
                else ExperimentJobStatus.RETRY_SCHEDULED
            )
            connection.execute(
                """
                UPDATE agent_lab_jobs
                SET status = ?, available_at = ?, leased_by = NULL, lease_expires_at = NULL,
                    last_error = ?, updated_at = ?
                WHERE job_id = ? AND leased_by = ? AND status = ?
                """,
                (
                    status.value,
                    _iso(now + timedelta(seconds=retry_delay_seconds)),
                    error[:4000],
                    _iso(now),
                    job_id,
                    worker_id,
                    ExperimentJobStatus.RUNNING.value,
                ),
            )
            connection.commit()
            return self.get_job(current.tenant_id, job_id) or current

    def _finish(
        self, job_id: str, worker_id: str, *, status: ExperimentJobStatus
    ) -> ExperimentJob:
        """在行锁保护下写入任务终态，确保一次回放只能由一个有效 Worker 结算。"""
        now = _now()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM agent_lab_jobs WHERE job_id = ? FOR UPDATE", (job_id,)
            ).fetchone()
            _assert_lease(row, worker_id, job_id)
            current = _job_from_row(row)
            connection.execute(
                """
                UPDATE agent_lab_jobs
                SET status = ?, leased_by = NULL, lease_expires_at = NULL, updated_at = ?
                WHERE job_id = ? AND leased_by = ? AND status = ?
                """,
                (status.value, _iso(now), job_id, worker_id, ExperimentJobStatus.RUNNING.value),
            )
            connection.commit()
            return self.get_job(current.tenant_id, job_id) or current

    def close(self) -> None:
        """短连接适配器没有持久连接池；保留关闭钩子以满足统一容器生命周期。"""

    def _connect(self):
        """按独立 Schema 创建短生命周期连接，禁止 Agent Lab 访问其他服务表。"""
        return connect_postgres(self._dsn, self._schema)


def _record_values(record: ExperimentRecord) -> tuple[str, str, str, str, str]:
    """将聚合转换为数据库参数，始终显式使用 UTC ISO 时间而非数据库本地时区。"""
    return (
        record.experiment_id,
        record.plan.tenant_id,
        record.status.value,
        record.model_dump_json(),
        _iso(record.updated_at),
    )


def _job_values(
    job: ExperimentJob,
) -> tuple[
    str,
    str,
    str,
    str,
    int,
    int,
    str,
    str | None,
    str | None,
    str | None,
    str,
    str,
]:
    """统一序列化任务和租约字段，避免 SQLite 与 PostgreSQL 存在不同的时间表示。"""
    return (
        job.job_id,
        job.experiment_id,
        job.tenant_id,
        job.status.value,
        job.attempt_count,
        job.max_attempts,
        _iso(job.available_at),
        job.leased_by,
        _iso(job.lease_expires_at) if job.lease_expires_at else None,
        job.last_error,
        _iso(job.created_at),
        _iso(job.updated_at),
    )


def _job_from_row(row) -> ExperimentJob:
    """从两种驱动的映射行重建强类型任务，非法持久化值会在 Worker 接流量前暴露。"""
    return ExperimentJob(
        job_id=row["job_id"],
        experiment_id=row["experiment_id"],
        tenant_id=row["tenant_id"],
        status=row["status"],
        attempt_count=row["attempt_count"],
        max_attempts=row["max_attempts"],
        available_at=_parse_time(row["available_at"]),
        leased_by=row["leased_by"],
        lease_expires_at=_parse_time(row["lease_expires_at"]) if row["lease_expires_at"] else None,
        last_error=row["last_error"],
        created_at=_parse_time(row["created_at"]),
        updated_at=_parse_time(row["updated_at"]),
    )


def _assert_lease(row, worker_id: str, job_id: str) -> None:
    """验证写入者仍持有运行中租约，防止网络分区后的双 Worker 破坏任务结论。"""
    if (
        row is None
        or row["status"] != ExperimentJobStatus.RUNNING.value
        or row["leased_by"] != worker_id
        or not row["lease_expires_at"]
        or _parse_time(row["lease_expires_at"]) <= _now()
    ):
        raise ValueError(f"job lease is not held: {job_id}")


def _now() -> datetime:
    """返回持久化租约使用的唯一 UTC 时钟来源。"""
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    """把时区感知时间规范化为可在两类数据库稳定比较的 ISO 文本。"""
    return value.astimezone(UTC).isoformat()


def _parse_time(value: str | datetime) -> datetime:
    """读取驱动返回的 ISO 文本或原生时间，并统一为时区感知 UTC。"""
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


_SQLITE_SCHEMA = """
PRAGMA journal_mode = WAL;
CREATE TABLE IF NOT EXISTS agent_lab_experiments (
    experiment_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    status TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS agent_lab_jobs (
    job_id TEXT PRIMARY KEY,
    experiment_id TEXT NOT NULL UNIQUE,
    tenant_id TEXT NOT NULL,
    status TEXT NOT NULL,
    attempt_count INTEGER NOT NULL,
    max_attempts INTEGER NOT NULL,
    available_at TEXT NOT NULL,
    leased_by TEXT,
    lease_expires_at TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (experiment_id) REFERENCES agent_lab_experiments(experiment_id)
);
CREATE INDEX IF NOT EXISTS idx_agent_lab_jobs_claim
    ON agent_lab_jobs (status, available_at);
"""

_POSTGRES_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS agent_lab_experiments (
        experiment_id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL,
        status TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS agent_lab_jobs (
        job_id TEXT PRIMARY KEY,
        experiment_id TEXT NOT NULL UNIQUE REFERENCES agent_lab_experiments(experiment_id),
        tenant_id TEXT NOT NULL,
        status TEXT NOT NULL,
        attempt_count INTEGER NOT NULL,
        max_attempts INTEGER NOT NULL,
        available_at TIMESTAMPTZ NOT NULL,
        leased_by TEXT,
        lease_expires_at TIMESTAMPTZ,
        last_error TEXT,
        created_at TIMESTAMPTZ NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_agent_lab_jobs_claim ON agent_lab_jobs (status, available_at)",
)
