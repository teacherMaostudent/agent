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
    """Continuously claim durable jobs and isolate exhausted exports in DLQ."""
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
    """Start the worker process using the same validated Governance configuration."""
    asyncio.run(run_worker(Settings()))
