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
    """在 Worker 启动时绑定模型发布监控回调；未绑定时 Activity
    必须失败而不是静默跳过。

    Bind the process-local activity implementation at worker start-up.
    """
    global _monitor
    _monitor = executor


@activity.defn(name="monitor_model_release")
async def monitor_model_release(tenant_id: str, release_id: str) -> dict[str, Any]:
    """Temporal Activity 调用已绑定的 Control Plane
    监控器；未绑定时失败以触发可观察重试。 回明确错误。 回明确错误。
    """
    if _monitor is None:
        raise RuntimeError("release monitor activity is not bound")
    return await _monitor(tenant_id, release_id)


@workflow.defn(name="ModelReleaseWorkflow")
class ModelReleaseWorkflow:
    @workflow.run
    async def run(self, tenant_id: str, release_id: str, interval_seconds: float) -> str:
        # The activity is retryable; the release monitor itself must remain
        # idempotent because a completed activity can be replayed by Temporal.
        """周期调用模型发布监控 Activity，直到发布进入终态；等待使用
        Temporal Timer，因而 Worker 重启后仍可恢复。
        """
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
        """创建专用事件循环并连接 Temporal；目标、命名空间和 Task Queue
        在实例生命周期内保持不变。
        """
        self.task_queue = task_queue
        self._loop = asyncio.new_event_loop()
        self._thread = Thread(target=self._loop.run_forever, daemon=True)
        self._thread.start()
        self._client = self._call(Client.connect(target, namespace=namespace))

    def start(self, tenant_id: str, release_id: str, interval_seconds: float) -> None:
        # A deterministic workflow id turns a duplicate API request into a
        # Temporal-level conflict instead of starting two release monitors.
        """以租户和发布 ID 组成确定性 Workflow
        ID，避免同一发布启动两个并行监控器。
        """
        self._call(
            self._client.start_workflow(
                ModelReleaseWorkflow.run,
                args=[tenant_id, release_id, interval_seconds],
                id=f"model-release/{tenant_id}/{release_id}",
                task_queue=self.task_queue,
            )
        )

    def close(self) -> None:
        """停止后台事件循环并等待线程退出；只释放编排客户端资源，不改变发布状态。"""
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)

    def _call(self, coroutine):
        """把协程提交到后台事件循环并同步取得结果；连接或启动错误原样返回给调用方。"""
        future: Future = asyncio.run_coroutine_threadsafe(coroutine, self._loop)
        return future.result(timeout=30)


async def run_worker() -> None:
    """连接指定 Temporal Namespace 并在固定 Task Queue 注册
    Workflow/Activity，连接失败阻止 Worker 启动。
    在产生外部副作用前返回明确错误。 在产生外部副作用前返回明确错误。
    """
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
    """启动独立 Temporal Worker 进程入口，并让进程退出码反映 Worker
    初始化或运行失败。
    """
    asyncio.run(run_worker())
