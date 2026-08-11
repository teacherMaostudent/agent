"""Temporal ingestion workflow ownership and worker registration."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from concurrent.futures import Future
from contextlib import suppress
from datetime import timedelta
from threading import Thread

from temporalio import activity, workflow
from temporalio.client import Client
from temporalio.common import RetryPolicy
from temporalio.exceptions import WorkflowAlreadyStartedError

_executor: Callable[[str], dict] | None = None


def bind_ingestion_executor(executor: Callable[[str], dict]) -> None:
    """绑定实际执行器到 Worker 进程；Workflow 定义本身不持有应用容器。"""
    global _executor
    _executor = executor


@activity.defn(name="execute_ingestion_job")
async def execute_ingestion_job(job_id: str) -> dict:
    """在线程中执行阻塞摄取任务，并把异常交给 Temporal 的重试策略。"""
    if _executor is None:
        raise RuntimeError("ingestion activity executor is not bound")
    return await asyncio.to_thread(_executor, job_id)


@workflow.defn(name="KnowledgeIngestionWorkflow")
class KnowledgeIngestionWorkflow:
    """Retry idempotent ingestion activities without duplicating source documents."""

    @workflow.run
    async def run(self, job_id: str, max_attempts: int) -> dict:
        """按稳定 job_id 调度活动；重试上限与持久化任务策略保持一致。"""
        return await workflow.execute_activity(
            "execute_ingestion_job",
            job_id,
            start_to_close_timeout=timedelta(hours=2),
            retry_policy=RetryPolicy(
                initial_interval=timedelta(seconds=2),
                backoff_coefficient=2,
                maximum_interval=timedelta(minutes=5),
                maximum_attempts=max_attempts,
                non_retryable_error_types=["ValueError"],
            ),
        )


class TemporalIngestionJobStore:
    """Submit durable ingestion jobs using deterministic workflow identifiers."""

    def __init__(self, backing, target: str, namespace: str, task_queue: str) -> None:
        """包装本地任务后端与 Temporal 调度参数，保持提交接口可替换。"""
        self.backing = backing
        self.task_queue = task_queue
        self._loop = asyncio.new_event_loop()
        self._thread = Thread(target=self._loop.run_forever, daemon=True)
        self._thread.start()
        self._client = self._call(Client.connect(target, namespace=namespace))

    def create(self, job):
        """先写任务事实记录，再幂等启动同 ID 的 Temporal Workflow。"""
        stored = self.backing.create(job)
        # Workflow id is deterministic. A duplicate start means another API
        # retry already owns the same durable job, so it is safely idempotent.
        with suppress(WorkflowAlreadyStartedError):
            self._call(
                self._client.start_workflow(
                    KnowledgeIngestionWorkflow.run,
                    args=[job.job_id, job.max_attempts],
                    id=f"ingestion/{job.tenant_id}/{job.job_id}",
                    task_queue=self.task_queue,
                )
            )
        return stored

    def get(self, job_id: str, tenant_id: str | None = None):
        """委托底层存储读取，保留其租户过滤语义。"""
        return self.backing.get(job_id, tenant_id)

    def close(self) -> None:
        """停止内部事件循环并关闭底层存储，避免进程退出时遗留连接。"""
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)
        self.backing.close()

    def _call(self, coroutine):
        """从同步 API 线程安全地等待 Temporal 协程，超时即向调用方暴露失败。"""
        future: Future = asyncio.run_coroutine_threadsafe(coroutine, self._loop)
        return future.result(timeout=30)
