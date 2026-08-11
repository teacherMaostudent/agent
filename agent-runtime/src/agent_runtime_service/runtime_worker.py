from __future__ import annotations

import asyncio

from temporalio.client import Client
from temporalio.worker import Worker

from agent_runtime_service.runtime.container import AgentRuntimeContainer
from agent_runtime_service.runtime.temporal_queue import (
    AgentRunWorkflow,
    bind_runtime_executor,
    execute_agent_run,
)


async def run_worker() -> None:
    """启动区域绑定的 Temporal Worker，并将本进程 Runtime 执行器注册为活动实现。

    工作流只传序列化提交，不传 Python callable；finally 确保 Worker 停止时释放所有
    SDK 客户端与存储连接。
    """
    container = AgentRuntimeContainer(build_async_queue=False)
    bind_runtime_executor(container._execute_submission)
    router = getattr(container.async_runs, "router", None)
    client = await Client.connect(
        router.target_for(container.settings.temporal_worker_region)
        if router is not None
        else container.settings.temporal_target,
        namespace=container.settings.temporal_namespace,
    )
    worker = Worker(
        client,
        task_queue=router.task_queue_for(
            container.settings.temporal_runtime_task_queue,
            container.settings.temporal_worker_region,
        )
        if router is not None
        else container.settings.temporal_runtime_task_queue,
        workflows=[AgentRunWorkflow],
        activities=[execute_agent_run],
    )
    try:
        await worker.run()
    finally:
        container.close()


def main() -> None:
    """提供控制台入口，统一由 asyncio 管理 Worker 生命周期。"""
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
