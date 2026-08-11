from __future__ import annotations

import asyncio

from temporalio.client import Client
from temporalio.worker import Worker

from app.ingestion.container import IngestionContainer
from app.ingestion.temporal_jobs import (
    KnowledgeIngestionWorkflow,
    bind_ingestion_executor,
    execute_ingestion_job,
)


async def run_worker() -> None:
    """注册摄取 Workflow/Activity 并消费指定任务队列；容器退出时关闭依赖。"""
    container = IngestionContainer(enable_temporal_dispatch=False)
    bind_ingestion_executor(container.execute_job)
    client = await Client.connect(
        container.settings.temporal_target,
        namespace=container.settings.temporal_namespace,
    )
    worker = Worker(
        client,
        task_queue=container.settings.temporal_ingestion_task_queue,
        workflows=[KnowledgeIngestionWorkflow],
        activities=[execute_ingestion_job],
    )
    try:
        await worker.run()
    finally:
        container.close()


def main() -> None:
    """提供 Temporal Worker 的同步命令行入口。"""
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
