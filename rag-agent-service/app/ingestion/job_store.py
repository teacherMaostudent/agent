import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Lock

from app.contracts.ingestion import IngestionJob, JobStatus


class IngestionJobStore:
    """SQLite development queue with an atomic claim operation.

    Production can replace this adapter with Kafka/Celery without changing the
    ingestion API or processor contract.
    """

    def __init__(self, path: Path) -> None:
        """初始化 SQLite 任务状态库及线程锁，仅用于单机或开发回退路径。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False, timeout=30)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS ingestion_jobs ("
            "job_id TEXT PRIMARY KEY, status TEXT NOT NULL, created_at TEXT NOT NULL, "
            "updated_at TEXT NOT NULL, next_attempt_at TEXT, lease_expires_at TEXT, payload TEXT NOT NULL)"
        )
        columns = {row[1] for row in self._conn.execute("PRAGMA table_info(ingestion_jobs)")}
        if "next_attempt_at" not in columns:
            self._conn.execute("ALTER TABLE ingestion_jobs ADD COLUMN next_attempt_at TEXT")
        if "lease_expires_at" not in columns:
            self._conn.execute("ALTER TABLE ingestion_jobs ADD COLUMN lease_expires_at TEXT")
        self._conn.commit()
        self._lock = Lock()

    def create(self, job: IngestionJob) -> IngestionJob:
        """持久化待处理任务；调用方负责以稳定 job_id 实现 API 幂等。"""
        with self._lock:
            self._write(job)
        return job

    def get(self, job_id: str, tenant_id: str | None = None) -> IngestionJob | None:
        """按可选租户过滤读取任务，避免仅凭 job_id 泄露跨租户执行状态。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT payload FROM ingestion_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        job = IngestionJob.model_validate_json(row[0]) if row else None
        if job is not None and tenant_id is not None and job.tenant_id != tenant_id:
            return None
        return job

    def claim_next(self) -> IngestionJob | None:
        """在 SQLite 立即事务中领取一个任务或过期租约，防止双 Worker 并发执行。"""
        now = datetime.now(UTC)
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            row = self._conn.execute(
                "SELECT job_id, payload FROM ingestion_jobs "
                "WHERE (status = ? AND (next_attempt_at IS NULL OR next_attempt_at <= ?)) "
                "OR (status = ? AND lease_expires_at < ?) ORDER BY created_at LIMIT 1",
                (
                    JobStatus.QUEUED.value,
                    now.isoformat(),
                    JobStatus.RUNNING.value,
                    now.isoformat(),
                ),
            ).fetchone()
            if row is None:
                self._conn.commit()
                return None
            job = IngestionJob.model_validate_json(row[1])
            job.status = JobStatus.RUNNING
            job.attempts += 1
            job.updated_at = now
            job.lease_expires_at = now + timedelta(minutes=5)
            job.next_attempt_at = None
            self._write(job, commit=False)
            self._conn.commit()
            return job

    def complete(self, job: IngestionJob, result: dict) -> IngestionJob:
        """提交终态及结果，清除租约和下次重试时间，终态不会再被领取。"""
        job.status = JobStatus.COMPLETED
        job.result = result
        job.error = ""
        job.lease_expires_at = None
        job.next_attempt_at = None
        job.updated_at = datetime.now(UTC)
        with self._lock:
            self._write(job)
        return job

    def fail(self, job: IngestionJob, error: str) -> IngestionJob:
        """记录可审计错误并按尝试次数指数退避；超过上限后转为 FAILED。"""
        now = datetime.now(UTC)
        job.status = JobStatus.QUEUED if job.attempts < job.max_attempts else JobStatus.FAILED
        job.error = error[:4000]
        job.updated_at = now
        job.lease_expires_at = None
        job.next_attempt_at = (
            now + timedelta(seconds=min(300, 2**job.attempts))
            if job.status == JobStatus.QUEUED
            else None
        )
        with self._lock:
            self._write(job)
        return job

    def _write(self, job: IngestionJob, commit: bool = True) -> None:
        """序列化完整 Job 快照；可由 claim 事务传入 commit=False 保持原子性。"""
        payload = job.model_dump_json()
        self._conn.execute(
            "INSERT INTO ingestion_jobs(job_id, status, created_at, updated_at, next_attempt_at, lease_expires_at, payload) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(job_id) DO UPDATE SET "
            "status=excluded.status, updated_at=excluded.updated_at, next_attempt_at=excluded.next_attempt_at, "
            "lease_expires_at=excluded.lease_expires_at, payload=excluded.payload",
            (
                job.job_id,
                job.status.value,
                job.created_at.isoformat(),
                job.updated_at.isoformat(),
                job.next_attempt_at.isoformat() if job.next_attempt_at else None,
                job.lease_expires_at.isoformat() if job.lease_expires_at else None,
                payload,
            ),
        )
        if commit:
            self._conn.commit()

    def close(self) -> None:
        """关闭开发用 SQLite 连接；调用后不可继续读取或抢占任务。"""
        with self._lock:
            self._conn.close()
