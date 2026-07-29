import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

from app.contracts.ingestion import IngestionJob, JobStatus


class IngestionJobStore:
    """SQLite development queue with an atomic claim operation.

    Production can replace this adapter with Kafka/Celery without changing the
    ingestion API or processor contract.
    """

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False, timeout=30)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS ingestion_jobs ("
            "job_id TEXT PRIMARY KEY, status TEXT NOT NULL, created_at TEXT NOT NULL, "
            "updated_at TEXT NOT NULL, payload TEXT NOT NULL)"
        )
        self._conn.commit()
        self._lock = Lock()

    def create(self, job: IngestionJob) -> IngestionJob:
        with self._lock:
            self._write(job)
        return job

    def get(self, job_id: str) -> IngestionJob | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT payload FROM ingestion_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        return IngestionJob.model_validate_json(row[0]) if row else None

    def claim_next(self) -> IngestionJob | None:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            row = self._conn.execute(
                "SELECT job_id, payload FROM ingestion_jobs "
                "WHERE status = ? ORDER BY created_at LIMIT 1",
                (JobStatus.QUEUED.value,),
            ).fetchone()
            if row is None:
                self._conn.commit()
                return None
            job = IngestionJob.model_validate_json(row[1])
            job.status = JobStatus.RUNNING
            job.attempts += 1
            job.updated_at = datetime.now(timezone.utc)
            self._write(job, commit=False)
            self._conn.commit()
            return job

    def complete(self, job: IngestionJob, result: dict) -> IngestionJob:
        job.status = JobStatus.COMPLETED
        job.result = result
        job.error = ""
        job.updated_at = datetime.now(timezone.utc)
        with self._lock:
            self._write(job)
        return job

    def fail(self, job: IngestionJob, error: str) -> IngestionJob:
        job.status = JobStatus.FAILED
        job.error = error[:4000]
        job.updated_at = datetime.now(timezone.utc)
        with self._lock:
            self._write(job)
        return job

    def _write(self, job: IngestionJob, commit: bool = True) -> None:
        payload = job.model_dump_json()
        self._conn.execute(
            "INSERT INTO ingestion_jobs(job_id, status, created_at, updated_at, payload) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(job_id) DO UPDATE SET "
            "status=excluded.status, updated_at=excluded.updated_at, payload=excluded.payload",
            (
                job.job_id,
                job.status.value,
                job.created_at.isoformat(),
                job.updated_at.isoformat(),
                payload,
            ),
        )
        if commit:
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()
