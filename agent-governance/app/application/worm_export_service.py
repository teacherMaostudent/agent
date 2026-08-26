"""Durable application service for retention-locked audit exports."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from app.core.config import Settings

WORM_EXPORT_JOB = "worm-export-job"
_ACTIVE = {"QUEUED", "RETRY", "RUNNING"}


def _now() -> datetime:
    """统一生成带 UTC 时区的当前时间，避免租约和重试在不同时区下产生歧义。"""
    return datetime.now(UTC)


class WormExportService:
    """Own WORM export requests while workers own storage side effects.

    API methods only persist intent. A separate worker claims jobs through CAS,
    performs chain verification/signing/upload, then stores an immutable result
    reference. This keeps slow object storage and KMS calls outside HTTP requests.
    """

    def __init__(self, repository: Any, settings: Settings) -> None:
        """保存作业仓储与导出限制配置；实际签名、验证和上传由独立 Worker 执行。"""
        self._repository = repository
        self._settings = settings

    async def create(self, tenant_id: str, requested_by: str) -> dict[str, Any]:
        """创建一个排队导出，并对同租户并发活动请求去重，避免重复 WORM 对象。"""
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
        """仅列出调用租户的导出请求和存储引用，保留租户数据边界。"""
        return await self._repository.list_documents(tenant_id, WORM_EXPORT_JOB, limit)

    async def get(self, tenant_id: str, job_id: str) -> dict[str, Any] | None:
        """读取单个导出作业且不接受调用方覆盖租户，防止 ID 枚举越权。"""
        return await self._repository.get_document(tenant_id, WORM_EXPORT_JOB, job_id)

    async def requeue(
        self, tenant_id: str, job_id: str, requested_by: str
    ) -> dict[str, Any] | None:
        """显式重新排队失败或死信导出，并重置其重试预算与旧租约。"""
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
        """在多个 Worker 副本间用数据库 CAS 租约领取一个到期作业。"""
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
        """仅在本 Worker 仍持有租约时持久化最终 WORM 引用，防止陈旧执行者写入。"""
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
        """按上限执行指数退避重试，并将耗尽尝试次数的作业隔离到死信队列。"""
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
    """解析已存 UTC 时间；格式异常的调度值按不存在处理，避免错误作业被提前执行。"""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except ValueError:
        return None
