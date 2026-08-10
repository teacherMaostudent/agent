"""Temporal ownership for release monitoring, not for release state.

The Control Plane repository is authoritative for release transitions.  This
workflow only drives periodic monitoring, so Temporal retries cannot invent or
overwrite a release decision after a worker failover.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from concurrent.futures import Future
from datetime import timedelta
from threading import Thread
from typing import Any

from temporalio import activity, workflow
from temporalio.client import Client
from temporalio.common import RetryPolicy
from temporalio.worker import Worker

_monitor: Callable[[str, str], Awaitable[dict[str, Any]]] | None = None


def bind_monitor(executor: Callable[[str, str], Awaitable[dict[str, Any]]]) -> None:
    """Bind the process-local activity implementation at worker start-up."""
    global _monitor
    _monitor = executor


@activity.defn(name="monitor_model_release")
async def monitor_model_release(tenant_id: str, release_id: str) -> dict[str, Any]:
    """Run the bounded monitor model release operation and surface failures."""
    if _monitor is None:
        raise RuntimeError("release monitor activity is not bound")
    return await _monitor(tenant_id, release_id)


@workflow.defn(name="ModelReleaseWorkflow")
class ModelReleaseWorkflow:
    @workflow.run
    async def run(self, tenant_id: str, release_id: str, interval_seconds: float) -> str:
        # The activity is retryable; the release monitor itself must remain
        # idempotent because a completed activity can be replayed by Temporal.
        """Perform run within the ModelReleaseWorkflow ownership boundary."""
        while True:
            release = await workflow.execute_activity(
                "monitor_model_release",
                args=[tenant_id, release_id],
                start_to_close_timeout=timedelta(minutes=2),
                retry_policy=RetryPolicy(
                    initial_interval=timedelta(seconds=2),
                    maximum_interval=timedelta(minutes=1),
                    maximum_attempts=10,
                ),
            )
            status = str(release.get("status", ""))
            if status not in {"CANARY_ACTIVE", "MONITORING"}:
                return status
            await workflow.sleep(timedelta(seconds=interval_seconds))


class TemporalReleaseOrchestrator:
    def __init__(self, target: str, namespace: str, task_queue: str) -> None:
        """Initialize TemporalReleaseOrchestrator dependencies and local state."""
        self.task_queue = task_queue
        self._loop = asyncio.new_event_loop()
        self._thread = Thread(target=self._loop.run_forever, daemon=True)
        self._thread.start()
        self._client = self._call(Client.connect(target, namespace=namespace))

    def start(self, tenant_id: str, release_id: str, interval_seconds: float) -> None:
        # A deterministic workflow id turns a duplicate API request into a
        # Temporal-level conflict instead of starting two release monitors.
        """Perform start within the TemporalReleaseOrchestrator ownership boundary."""
        self._call(
            self._client.start_workflow(
                ModelReleaseWorkflow.run,
                args=[tenant_id, release_id, interval_seconds],
                id=f"model-release/{tenant_id}/{release_id}",
                task_queue=self.task_queue,
            )
        )

    def close(self) -> None:
        """Perform close within the TemporalReleaseOrchestrator ownership boundary."""
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)

    def _call(self, coroutine):
        """Internal helper for TemporalReleaseOrchestrator; preserve its caller-facing invariant."""
        future: Future = asyncio.run_coroutine_threadsafe(coroutine, self._loop)
        return future.result(timeout=30)


async def run_worker() -> None:
    """Run the bounded run worker operation and surface failures."""
    from app.container import AppContainer
    from app.core.config import Settings

    settings = Settings()
    container = AppContainer(settings, build_orchestrator=False)
    await container.start()
    bind_monitor(container.model_releases.monitor)
    client = await Client.connect(settings.temporal_target, namespace=settings.temporal_namespace)
    worker = Worker(
        client,
        task_queue=settings.temporal_task_queue,
        workflows=[ModelReleaseWorkflow],
        activities=[monitor_model_release],
    )
    try:
        await worker.run()
    finally:
        await container.stop()


def main() -> None:
    """Perform main within the module ownership boundary."""
    asyncio.run(run_worker())
