from __future__ import annotations

import asyncio
from collections.abc import Callable
from concurrent.futures import Future
from datetime import timedelta
from threading import Thread

from temporalio import activity, workflow
from temporalio.client import Client
from temporalio.common import RetryPolicy
from temporalio.exceptions import WorkflowAlreadyStartedError

_executor: Callable[[str], dict] | None = None


def bind_ingestion_executor(executor: Callable[[str], dict]) -> None:
    global _executor
    _executor = executor


@activity.defn(name="execute_ingestion_job")
async def execute_ingestion_job(job_id: str) -> dict:
    if _executor is None:
        raise RuntimeError("ingestion activity executor is not bound")
    return await asyncio.to_thread(_executor, job_id)


@workflow.defn(name="KnowledgeIngestionWorkflow")
class KnowledgeIngestionWorkflow:
    @workflow.run
    async def run(self, job_id: str, max_attempts: int) -> dict:
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
    def __init__(self, backing, target: str, namespace: str, task_queue: str) -> None:
        self.backing = backing
        self.task_queue = task_queue
        self._loop = asyncio.new_event_loop()
        self._thread = Thread(target=self._loop.run_forever, daemon=True)
        self._thread.start()
        self._client = self._call(Client.connect(target, namespace=namespace))

    def create(self, job):
        stored = self.backing.create(job)
        try:
            self._call(
                self._client.start_workflow(
                    KnowledgeIngestionWorkflow.run,
                    args=[job.job_id, job.max_attempts],
                    id=f"ingestion/{job.tenant_id}/{job.job_id}",
                    task_queue=self.task_queue,
                )
            )
        except WorkflowAlreadyStartedError:
            pass
        return stored

    def get(self, job_id: str, tenant_id: str | None = None):
        return self.backing.get(job_id, tenant_id)

    def close(self) -> None:
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)
        self.backing.close()

    def _call(self, coroutine):
        future: Future = asyncio.run_coroutine_threadsafe(coroutine, self._loop)
        return future.result(timeout=30)
