"""Local durable queue for asynchronous Runtime requests.

This development adapter persists submissions before worker dispatch and
deduplicates by tenant/request id.  Production uses Temporal/Kafka-compatible
execution paths rather than treating an in-process pool as durable HA.
"""

from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import Any, Callable
from uuid import uuid4


class AsyncRunQueue:
    """Durable local queue behind the asynchronous Runtime API."""

    def __init__(self, path: Path, execute: Callable[[dict[str, Any]], dict]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(path, check_same_thread=False, timeout=30)
        self._connection.row_factory = sqlite3.Row
        self._lock = Lock()
        self._execute = execute
        self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="agent-runtime")
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute(
            """CREATE TABLE IF NOT EXISTS runtime_jobs(
            run_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, request_id TEXT NOT NULL,
            status TEXT NOT NULL, submission_json TEXT NOT NULL, result_json TEXT NOT NULL DEFAULT '{}',
            error TEXT NOT NULL DEFAULT '', cancel_requested INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            UNIQUE(tenant_id, request_id))"""
        )
        self._connection.execute(
            "UPDATE runtime_jobs SET status = 'QUEUED' WHERE status = 'RUNNING'"
        )
        self._connection.commit()
        recover = self._connection.execute(
            "SELECT submission_json FROM runtime_jobs WHERE status = 'QUEUED' ORDER BY created_at"
        ).fetchall()
        for row in recover:
            self._pool.submit(self._run, json.loads(row["submission_json"]))

    def submit(self, submission: dict[str, Any]) -> dict[str, Any]:
        """Persist first, then schedule; retries with the same request id are replays."""
        now = datetime.now(UTC).isoformat()
        run_id = submission.setdefault("run_id", f"run_{uuid4().hex}")
        with self._lock:
            existing = self._connection.execute(
                "SELECT * FROM runtime_jobs WHERE tenant_id = ? AND request_id = ?",
                (submission["tenant_id"], submission["request_id"]),
            ).fetchone()
            if existing:
                return self._row(existing)
            self._connection.execute(
                "INSERT INTO runtime_jobs(run_id, tenant_id, request_id, status, submission_json, created_at, updated_at) "
                "VALUES (?, ?, ?, 'QUEUED', ?, ?, ?)",
                (run_id, submission["tenant_id"], submission["request_id"], json.dumps(submission, ensure_ascii=False), now, now),
            )
            self._connection.commit()
        self._pool.submit(self._run, submission)
        return self.get(submission["tenant_id"], run_id) or {}

    def get(self, tenant_id: str, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM runtime_jobs WHERE tenant_id = ? AND run_id = ?",
                (tenant_id, run_id),
            ).fetchone()
        return self._row(row) if row else None

    def cancel(self, tenant_id: str, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            self._connection.execute(
                "UPDATE runtime_jobs SET cancel_requested = 1, status = CASE WHEN status = 'QUEUED' THEN 'CANCELLED' ELSE status END, "
                "updated_at = ? WHERE tenant_id = ? AND run_id = ?",
                (datetime.now(UTC).isoformat(), tenant_id, run_id),
            )
            self._connection.commit()
        return self.get(tenant_id, run_id)

    def close(self) -> None:
        self._pool.shutdown(wait=True, cancel_futures=False)
        with self._lock:
            self._connection.close()

    def _run(self, submission: dict[str, Any]) -> None:
        run_id = submission["run_id"]
        with self._lock:
            row = self._connection.execute(
                "SELECT cancel_requested, status FROM runtime_jobs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None or row["cancel_requested"] or row["status"] == "CANCELLED":
                return
            self._connection.execute(
                "UPDATE runtime_jobs SET status = 'RUNNING', updated_at = ? WHERE run_id = ?",
                (datetime.now(UTC).isoformat(), run_id),
            )
            self._connection.commit()
        try:
            result = self._execute(submission)
            status, error = str(result.get("status", "COMPLETED")), ""
        except Exception as exc:
            result, status, error = {}, "FAILED", f"{type(exc).__name__}: {exc}"[:4000]
        with self._lock:
            self._connection.execute(
                "UPDATE runtime_jobs SET status = ?, result_json = ?, error = ?, updated_at = ? WHERE run_id = ?",
                (status, json.dumps(result, ensure_ascii=False), error, datetime.now(UTC).isoformat(), run_id),
            )
            self._connection.commit()

    @staticmethod
    def _row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "run_id": row["run_id"], "request_id": row["request_id"], "status": row["status"],
            "result": json.loads(row["result_json"]), "error": row["error"],
            "cancel_requested": bool(row["cancel_requested"]),
            "created_at": row["created_at"], "updated_at": row["updated_at"],
        }
