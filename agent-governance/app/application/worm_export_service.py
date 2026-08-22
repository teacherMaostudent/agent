"""Durable application service for retention-locked audit exports."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from app.core.config import Settings

WORM_EXPORT_JOB = "worm-export-job"
_ACTIVE = {"QUEUED", "RETRY", "RUNNING"}


def _now() -> datetime:
    return datetime.now(UTC)


class WormExportService:
    """Own WORM export requests while workers own storage side effects.

    API methods only persist intent. A separate worker claims jobs through CAS,
    performs chain verification/signing/upload, then stores an immutable result
    reference. This keeps slow object storage and KMS calls outside HTTP requests.
    """

    def __init__(self, repository: Any, settings: Settings) -> None:
        self._repository = repository
        self._settings = settings

    async def create(self, tenant_id: str, requested_by: str) -> dict[str, Any]:
        """Create one queued export, deduplicating concurrent active requests."""
        existing = await self._repository.list_documents(tenant_id, WORM_EXPORT_JOB, 1_000)
        active = next((item for item in existing if item.get("status") in _ACTIVE), None)
        if active:
            return active
        now = _now().isoformat()
        job = {
            "job_id": f"worm_{uuid4().hex}",
            "tenant_id": tenant_id,
            "status": "QUEUED",
            "requested_by": requested_by,
            "requested_at": now,
            "updated_at": now,
            "attempts": 0,
            "max_attempts": self._settings.worm_export_max_attempts,
            "next_attempt_at": now,
            "lease_owner": None,
            "lease_expires_at": None,
            "result": None,
            "last_error": None,
        }
        return await self._repository.upsert_document(
            tenant_id, WORM_EXPORT_JOB, job["job_id"], job
        )

    async def list(self, tenant_id: str, limit: int = 100) -> list[dict[str, Any]]:
        """List only the caller tenant's export requests and storage references."""
        return await self._repository.list_documents(tenant_id, WORM_EXPORT_JOB, limit)

    async def get(self, tenant_id: str, job_id: str) -> dict[str, Any] | None:
        """Read a single export without allowing a caller-supplied tenant override."""
        return await self._repository.get_document(tenant_id, WORM_EXPORT_JOB, job_id)

    async def requeue(
        self, tenant_id: str, job_id: str, requested_by: str
    ) -> dict[str, Any] | None:
        """Explicitly requeue a failed/DLQ export and reset its retry budget."""
        current = await self.get(tenant_id, job_id)
        if not current:
            return None
        if current.get("status") not in {"FAILED", "DLQ"}:
            raise ValueError("only FAILED or DLQ exports can be requeued")
        replacement = {
            **current,
            "status": "QUEUED",
            "attempts": 0,
            "next_attempt_at": _now().isoformat(),
            "lease_owner": None,
            "lease_expires_at": None,
            "last_error": None,
            "requeued_by": requested_by,
            "updated_at": _now().isoformat(),
        }
        swapped = await self._repository.compare_and_swap_document(
            tenant_id, WORM_EXPORT_JOB, job_id, current, replacement
        )
        return replacement if swapped else await self.get(tenant_id, job_id)

    async def claim(self, worker_id: str) -> dict[str, Any] | None:
        """Acquire one due job using a database CAS lease across worker replicas."""
        now = _now()
        candidates = await self._repository.list_documents_all_tenants(WORM_EXPORT_JOB, 1_000)
        for current in candidates:
            status = str(current.get("status", ""))
            lease_expires = _parse_time(current.get("lease_expires_at"))
            next_attempt = _parse_time(current.get("next_attempt_at")) or now
            recoverable_running = status == "RUNNING" and lease_expires and lease_expires <= now
            if not (status in {"QUEUED", "RETRY"} or recoverable_running):
                continue
            if next_attempt > now:
                continue
            replacement = {
                **current,
                "status": "RUNNING",
                "attempts": int(current.get("attempts", 0)) + 1,
                "lease_owner": worker_id,
                "lease_expires_at": (
                    now + timedelta(seconds=self._settings.worm_export_lease_seconds)
                ).isoformat(),
                "updated_at": now.isoformat(),
            }
            if await self._repository.compare_and_swap_document(
                str(current["tenant_id"]),
                WORM_EXPORT_JOB,
                str(current["job_id"]),
                current,
                replacement,
            ):
                return replacement
        return None

    async def complete(self, job: dict[str, Any], result: dict[str, Any]) -> None:
        """Persist the final WORM reference only while this worker still owns the lease."""
        current = await self.get(str(job["tenant_id"]), str(job["job_id"]))
        if not current or current.get("lease_owner") != job.get("lease_owner"):
            return
        replacement = {
            **current,
            "status": "COMPLETED",
            "result": result,
            "lease_owner": None,
            "lease_expires_at": None,
            "completed_at": _now().isoformat(),
            "updated_at": _now().isoformat(),
        }
        await self._repository.compare_and_swap_document(
            str(job["tenant_id"]), WORM_EXPORT_JOB, str(job["job_id"]), current, replacement
        )

    async def fail(self, job: dict[str, Any], error: Exception) -> None:
        """Schedule bounded exponential retry, then isolate exhausted jobs in DLQ."""
        current = await self.get(str(job["tenant_id"]), str(job["job_id"]))
        if not current or current.get("lease_owner") != job.get("lease_owner"):
            return
        attempts = int(current.get("attempts", 1))
        exhausted = attempts >= int(current.get("max_attempts", 1))
        delay = min(3600, 2 ** min(attempts, 10))
        replacement = {
            **current,
            "status": "DLQ" if exhausted else "RETRY",
            "next_attempt_at": (_now() + timedelta(seconds=delay)).isoformat(),
            "lease_owner": None,
            "lease_expires_at": None,
            "last_error": str(error)[:2_000],
            "updated_at": _now().isoformat(),
        }
        await self._repository.compare_and_swap_document(
            str(job["tenant_id"]), WORM_EXPORT_JOB, str(job["job_id"]), current, replacement
        )


def _parse_time(value: Any) -> datetime | None:
    """Parse stored UTC timestamps; malformed scheduling data fails closed as absent."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except ValueError:
        return None
