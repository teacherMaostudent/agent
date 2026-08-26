"""Lease-based worker for Governance WORM export jobs."""

from __future__ import annotations

import asyncio
import os
import socket
from uuid import uuid4

from app.container import AppContainer
from app.core.config import Settings
from app.infrastructure.worm_exporter import export_tenant


async def run_worker(settings: Settings) -> None:
    """持续领取持久导出作业，并将耗尽重试预算的导出隔离至死信队列。"""
    container = AppContainer(settings)
    await container.start()
    worker_id = f"{socket.gethostname()}-{os.getpid()}-{uuid4().hex[:8]}"
    while True:
        job = await container.worm_exports.claim(worker_id)
        if job is None:
            await asyncio.sleep(settings.worm_export_poll_seconds)
            continue
        try:
            result = await export_tenant(settings, str(job["tenant_id"]), container.repository)
            await container.worm_exports.complete(job, result)
        except Exception as exc:
            await container.worm_exports.fail(job, exc)


def main() -> None:
    """使用同一份已校验 Governance 配置启动 WORM 导出 Worker 进程。"""
    asyncio.run(run_worker(Settings()))
