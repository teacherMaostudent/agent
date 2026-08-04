"""Temporal-backed durable Agent execution with region-aware submission.

The workflow id is tenant and run scoped, while the task queue is region
scoped.  This separation keeps retries idempotent when submission fails over
to another Temporal cluster.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from concurrent.futures import Future
from datetime import timedelta
from threading import Thread
from typing import Any
from uuid import uuid4

from temporalio import activity, workflow
from temporalio.client import Client, WorkflowExecutionStatus
from temporalio.common import RetryPolicy
from temporalio.exceptions import TemporalError, WorkflowAlreadyStartedError

from app.runtime.temporal_routing import TemporalTargetRouter

# Activities run in a separately deployed Worker process.  The process binds
# the local Runtime executor at startup rather than serialising Python callables
# into a Temporal payload.
_executor: Callable[[dict[str, Any]], dict] | None = None


def bind_runtime_executor(executor: Callable[[dict[str, Any]], dict]) -> None:
    global _executor
    _executor = executor


@activity.defn(name="execute_agent_run")
async def execute_agent_run(submission: dict[str, Any]) -> dict:
    if _executor is None:
        raise RuntimeError("runtime activity executor is not bound")
    return await asyncio.to_thread(_executor, submission)


@workflow.defn(name="AgentRunWorkflow")
class AgentRunWorkflow:
    @workflow.run
    async def run(self, submission: dict[str, Any]) -> dict:
        return await workflow.execute_activity(
            "execute_agent_run",
            submission,
            start_to_close_timeout=timedelta(hours=1),
            retry_policy=RetryPolicy(
                initial_interval=timedelta(seconds=2),
                backoff_coefficient=2,
                maximum_interval=timedelta(minutes=2),
                maximum_attempts=5,
                non_retryable_error_types=["SnapshotCompileError", "ValueError"],
            ),
        )


class TemporalRunQueue:
    """Runtime queue backed by Temporal durable execution."""

    def __init__(
        self, target: str, namespace: str, task_queue: str, region_targets: str = ""
    ) -> None:
        self.target = target
        self.namespace = namespace
        self.task_queue = task_queue
        self.router = TemporalTargetRouter(target, region_targets)
        self._loop = asyncio.new_event_loop()
        self._thread = Thread(target=self._loop.run_forever, daemon=True)
        self._thread.start()
        self._clients = {target: self._call(Client.connect(target, namespace=namespace))}
        for regional_target in self.router.targets.values():
            if regional_target not in self._clients:
                self._clients[regional_target] = self._call(
                    Client.connect(regional_target, namespace=namespace)
                )

    def submit(self, submission: dict[str, Any]) -> dict[str, Any]:
        run_id = submission.setdefault("run_id", f"run_{uuid4().hex}")
        workflow_id = self._workflow_id(submission["tenant_id"], run_id)
        # Every candidate uses the same workflow id.  A retry after a network
        # timeout therefore resolves to WorkflowAlreadyStarted instead of
        # creating a second agent run in another region.
        region = submission.get("data_region")
        last_error: Exception | None = None
        for target in self.router.candidates(region):
            client = self._clients[target]
            try:
                self._call(
                    client.start_workflow(
                        AgentRunWorkflow.run,
                        submission,
                        id=workflow_id,
                        task_queue=self.router.task_queue_for(self.task_queue, region),
                    )
                )
                break
            except WorkflowAlreadyStartedError:
                break
            except (TemporalError, TimeoutError) as exc:
                last_error = exc
        else:
            raise RuntimeError("all Temporal region targets are unavailable") from last_error
        return {
            "run_id": run_id,
            "request_id": submission["request_id"],
            "status": "QUEUED",
            "result": {},
            "error": "",
            "cancel_requested": False,
        }

    def get(self, tenant_id: str, run_id: str) -> dict[str, Any] | None:
        handle = None
        description = None
        for client in self._clients.values():
            candidate = client.get_workflow_handle(self._workflow_id(tenant_id, run_id))
            try:
                description = self._call(candidate.describe())
                handle = candidate
                break
            except (TemporalError, TimeoutError):
                continue
        if description is None or handle is None:
            return None
        status = _status(description.status)
        result: dict[str, Any] = {}
        error = ""
        if description.status == WorkflowExecutionStatus.COMPLETED:
            try:
                result = self._call(handle.result())
            except (TemporalError, TimeoutError) as exc:
                error = f"{type(exc).__name__}: {exc}"[:4000]
        return {
            "run_id": run_id,
            "request_id": "",
            "status": status,
            "result": result,
            "error": error,
            "cancel_requested": status == "CANCELLED",
        }

    def cancel(self, tenant_id: str, run_id: str) -> dict[str, Any] | None:
        for client in self._clients.values():
            handle = client.get_workflow_handle(self._workflow_id(tenant_id, run_id))
            try:
                self._call(handle.cancel())
                break
            except (TemporalError, TimeoutError):
                continue
        else:
            return None
        return self.get(tenant_id, run_id)

    def close(self) -> None:
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)

    def _call(self, coroutine) -> Any:
        future: Future = asyncio.run_coroutine_threadsafe(coroutine, self._loop)
        return future.result(timeout=30)

    @staticmethod
    def _workflow_id(tenant_id: str, run_id: str) -> str:
        return f"agent-run/{tenant_id}/{run_id}"


def _status(status: WorkflowExecutionStatus) -> str:
    return {
        WorkflowExecutionStatus.RUNNING: "RUNNING",
        WorkflowExecutionStatus.COMPLETED: "COMPLETED",
        WorkflowExecutionStatus.FAILED: "FAILED",
        WorkflowExecutionStatus.CANCELED: "CANCELLED",
        WorkflowExecutionStatus.TERMINATED: "CANCELLED",
        WorkflowExecutionStatus.TIMED_OUT: "FAILED",
    }.get(status, "QUEUED")
