"""Agent Lab 的 Temporal 调度：工作流只编排任务，不保存实验结论或绕过数据库租约。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from concurrent.futures import Future
from datetime import timedelta
from threading import Thread
from typing import Any

from temporalio import activity, workflow
from temporalio.client import Client
from temporalio.exceptions import WorkflowAlreadyStartedError

_executor: Callable[[str], dict[str, Any]] | None = None


def bind_experiment_executor(executor: Callable[[str], dict[str, Any]]) -> None:
    """在独立 Worker 进程启动时绑定本地执行器，Python callable 永不进入 Temporal payload。"""
    global _executor
    _executor = executor


@activity.defn(name="execute_agent_lab_experiment")
async def execute_agent_lab_experiment(job_id: str) -> dict[str, Any]:
    """在线程中运行同步回放服务；未绑定执行器代表 Worker 部署损坏，应立即失败。"""
    if _executor is None:
        raise RuntimeError("Agent Lab activity executor is not bound")
    return await asyncio.to_thread(_executor, job_id)


@workflow.defn(name="AgentLabExperimentWorkflow")
class AgentLabExperimentWorkflow:
    """以稳定工作流 ID 串行驱动一个实验任务，重试节奏由持久化任务状态决定。"""

    @workflow.run
    async def run(self, job_id: str) -> str:
        """执行活动并等待持久化退避；DLQ 与完成都作为可审计终态返回。"""
        while True:
            outcome = await workflow.execute_activity(
                "execute_agent_lab_experiment",
                job_id,
                start_to_close_timeout=timedelta(hours=2),
            )
            status = str(outcome.get("status", "DLQ"))
            if status != "RETRY_SCHEDULED":
                return status
            await workflow.sleep(timedelta(seconds=max(1, int(outcome["retryDelaySeconds"]))))


class TemporalExperimentQueue:
    """将已持久化任务提交为幂等 Temporal workflow；数据库仍是任务状态的真源。"""

    def __init__(self, target: str, namespace: str, task_queue: str) -> None:
        """在 API 进程建立专用事件循环和 Temporal 客户端，连接失败拒绝接收提交。"""
        self._task_queue = task_queue
        self._loop = asyncio.new_event_loop()
        self._thread = Thread(target=self._loop.run_forever, daemon=True)
        self._thread.start()
        self._client = self._call(Client.connect(target, namespace=namespace))

    def submit(self, job_id: str) -> None:
        """以任务 ID 构造确定工作流键，网络重试只会命中同一调度实例。"""
        try:
            self._call(
                self._client.start_workflow(
                    AgentLabExperimentWorkflow.run,
                    job_id,
                    id=f"agent-lab/{job_id}",
                    task_queue=self._task_queue,
                )
            )
        except WorkflowAlreadyStartedError:
            return

    def close(self) -> None:
        """停止 API 专用事件循环；Temporal 客户端没有需要显式释放的业务状态。"""
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)

    def _call(self, coroutine) -> Any:
        """跨线程等待 Temporal 异步调用；超时留给 API 转换为失败响应。"""
        future: Future[Any] = asyncio.run_coroutine_threadsafe(coroutine, self._loop)
        return future.result(timeout=30)


class LocalExperimentQueue:
    """本地开发队列：保留相同提交接口，但不宣称跨进程持久调度或高可用。"""

    def __init__(self, executor: Callable[[str], dict[str, Any]]) -> None:
        """保存测试期执行器，便于契约测试验证提交语义而无需 Temporal 服务。"""
        self._executor = executor

    def submit(self, job_id: str) -> None:
        """本地同步执行任务，明确只用于开发；生产 Settings 会拒绝此模式。"""
        self._executor(job_id)

    def close(self) -> None:
        """本地队列不拥有后台资源，保留统一生命周期接口。"""
