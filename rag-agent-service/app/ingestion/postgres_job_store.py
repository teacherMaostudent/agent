from __future__ import annotations

from datetime import UTC, datetime, timedelta

from platform_infra.postgres import connect_postgres

from app.contracts.ingestion import IngestionJob, JobStatus


class PostgresIngestionJobStore:
    """HA-safe ingestion state with SKIP LOCKED worker claims."""

    def __init__(self, dsn: str, schema: str) -> None:
        """连接 PostgreSQL 并创建支持多 Worker 竞争领取的摄取任务表。"""
        self._dsn = dsn
        self._schema = schema
        with connect_postgres(dsn, schema) as connection:
            connection.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
            connection.commit()
        with connect_postgres(dsn, schema) as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS ingestion_jobs(
                job_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, status TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL,
                next_attempt_at TIMESTAMPTZ, lease_expires_at TIMESTAMPTZ,
                payload TEXT NOT NULL)"""
            )
            connection.execute(
                """CREATE INDEX IF NOT EXISTS ingestion_jobs_claim_idx
                ON ingestion_jobs(status, next_attempt_at, created_at)"""
            )
            connection.commit()

    def create(self, job: IngestionJob) -> IngestionJob:
        """将待处理任务写入 PostgreSQL，作为多副本 Worker 的共享事实来源。"""
        self._write(job)
        return job

    def get(self, job_id: str, tenant_id: str | None = None) -> IngestionJob | None:
        """读取任务，并在查询结果返回前执行租户隔离检查。"""
        with connect_postgres(self._dsn, self._schema) as connection:
            row = connection.execute(
                "SELECT payload FROM ingestion_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        job = IngestionJob.model_validate_json(row["payload"]) if row else None
        if job is not None and tenant_id is not None and job.tenant_id != tenant_id:
            return None
        return job

    def claim_next(self) -> IngestionJob | None:
        """使用 FOR UPDATE SKIP LOCKED 领取任务，避免多个 Worker 等待同一行锁。"""
        now = datetime.now(UTC)
        with connect_postgres(self._dsn, self._schema) as connection:
            row = connection.execute(
                """SELECT payload FROM ingestion_jobs
                WHERE (status = ? AND (next_attempt_at IS NULL OR next_attempt_at <= ?))
                   OR (status = ? AND lease_expires_at < ?)
                ORDER BY created_at LIMIT 1 FOR UPDATE SKIP LOCKED""",
                (
                    JobStatus.QUEUED.value,
                    now.isoformat(),
                    JobStatus.RUNNING.value,
                    now.isoformat(),
                ),
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            job = IngestionJob.model_validate_json(row["payload"])
            job.status = JobStatus.RUNNING
            job.attempts += 1
            job.updated_at = now
            job.lease_expires_at = now + timedelta(minutes=5)
            job.next_attempt_at = None
            self._write(job, connection=connection)
            connection.commit()
            return job

    def complete(self, job: IngestionJob, result: dict) -> IngestionJob:
        """写入成功终态，释放租约并保留结果供 API 和审计读取。"""
        job.status = JobStatus.COMPLETED
        job.result = result
        job.error = ""
        job.lease_expires_at = None
        job.next_attempt_at = None
        job.updated_at = datetime.now(UTC)
        self._write(job)
        return job

    def fail(self, job: IngestionJob, error: str) -> IngestionJob:
        """持久化失败原因并计算下一次尝试；达到上限后永久失败。"""
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
        self._write(job)
        return job

    def _write(self, job: IngestionJob, connection=None) -> None:
        """写入任务快照；复用传入连接时由上层 claim 事务统一提交。"""
        owned = connection is None
        target = connection or connect_postgres(self._dsn, self._schema)
        try:
            target.execute(
                """INSERT INTO ingestion_jobs(
                job_id, tenant_id, status, created_at, updated_at,
                next_attempt_at, lease_expires_at, payload)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET status=excluded.status,
                updated_at=excluded.updated_at, next_attempt_at=excluded.next_attempt_at,
                lease_expires_at=excluded.lease_expires_at, payload=excluded.payload""",
                (
                    job.job_id,
                    job.tenant_id,
                    job.status.value,
                    job.created_at.isoformat(),
                    job.updated_at.isoformat(),
                    job.next_attempt_at.isoformat() if job.next_attempt_at else None,
                    job.lease_expires_at.isoformat() if job.lease_expires_at else None,
                    job.model_dump_json(),
                ),
            )
            if owned:
                target.commit()
        finally:
            if owned:
                target.close()

    def close(self) -> None:
        """PostgreSQL 连接按操作获取，无需保留或关闭长连接。"""
        return None
