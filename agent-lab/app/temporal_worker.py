"""Agent Lab Temporal Worker 入口：独立于 API 进程运行，确保长回放不会占用 Web 容量。"""

from __future__ import annotations

import asyncio

from temporalio.client import Client
from temporalio.worker import Worker

from app.container import AgentLabContainer
from app.main_settings import Settings
from app.temporal_queue import (
    AgentLabExperimentWorkflow,
    bind_experiment_executor,
    execute_agent_lab_experiment,
)


async def run_worker() -> None:
    """创建 Worker 专用容器、绑定本地活动实现并持续消费隔离任务队列。"""
    settings = Settings()
    container = AgentLabContainer(settings, build_queue=False)
    bind_experiment_executor(container.worker.execute)
    client = await Client.connect(settings.temporal_target, namespace=settings.temporal_namespace)
    worker = Worker(
        client,
        task_queue=settings.temporal_task_queue,
        workflows=[AgentLabExperimentWorkflow],
        activities=[execute_agent_lab_experiment],
    )
    try:
        await worker.run()
    finally:
        container.close()


def main() -> None:
    """提供控制台入口，使生产 Compose 可以使用同一镜像启动独立 Worker 工作负载。"""
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
