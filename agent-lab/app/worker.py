"""回放任务执行器：租约、重试与 DLQ 的权威状态仍由 Repository 持久化。"""

from __future__ import annotations

from typing import Any

from app.models import ExperimentJobStatus
from app.repository import ExperimentRepositoryPort
from app.service import AgentLabService, RetryableExperimentError


class AgentLabWorker:
    """领取单个任务并驱动回放；Temporal 负责持久调度，不替代数据库租约。"""

    def __init__(
        self,
        repository: ExperimentRepositoryPort,
        service: AgentLabService,
        *,
        worker_id: str,
        lease_seconds: int,
        retry_initial_seconds: int,
        retry_max_seconds: int,
    ) -> None:
        """注入任务存储和应用服务，避免 Worker 直接调用 Runtime 或 Governance。"""
        self._repository = repository
        self._service = service
        self._worker_id = worker_id
        self._lease_seconds = lease_seconds
        self._retry_initial_seconds = retry_initial_seconds
        self._retry_max_seconds = retry_max_seconds

    def execute(self, job_id: str) -> dict[str, Any]:
        """领取并执行一个任务；所有下游异常受限重试，超过预算后以持久化 DLQ 收口。"""
        job = self._repository.get_job_for_worker(job_id)
        if job is None:
            raise KeyError(f"experiment job not found: {job_id}")
        claimed = self._repository.claim(job_id, self._worker_id, self._lease_seconds)
        if claimed is None:
            return {"status": job.status.value, "retryDelaySeconds": 0}
        try:
            self._service.execute_claimed(claimed)
            completed = self._repository.complete(claimed.job_id, self._worker_id)
            return {"status": completed.status.value, "retryDelaySeconds": 0}
        except RetryableExperimentError as exc:
            retried = self._repository.retry_or_dead_letter(
                claimed.job_id,
                self._worker_id,
                str(exc),
                self._retry_delay(claimed.attempt_count),
            )
            if retried.status == ExperimentJobStatus.RETRY_SCHEDULED:
                self._service.mark_retry(retried, str(exc))
            else:
                self._service.mark_dead_letter(retried, str(exc))
            return {
                "status": retried.status.value,
                "retryDelaySeconds": self._retry_delay(claimed.attempt_count),
            }
        except Exception as exc:
            recovered = self._repository.retry_or_dead_letter(
                claimed.job_id,
                self._worker_id,
                f"unexpected: {exc}",
                self._retry_delay(claimed.attempt_count),
            )
            if recovered.status == ExperimentJobStatus.RETRY_SCHEDULED:
                self._service.mark_retry(recovered, str(exc))
            else:
                self._service.mark_dead_letter(recovered, str(exc))
            return {
                "status": recovered.status.value,
                "retryDelaySeconds": (
                    self._retry_delay(claimed.attempt_count)
                    if recovered.status == ExperimentJobStatus.RETRY_SCHEDULED
                    else 0
                ),
            }

    def _retry_delay(self, attempt_count: int) -> int:
        """计算有上限的指数退避，避免故障下游被大量实验 Worker 同时重试。"""
        return min(
            self._retry_max_seconds,
            self._retry_initial_seconds * (2 ** (attempt_count - 1)),
        )
