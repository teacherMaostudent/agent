from __future__ import annotations

import asyncio
from contextlib import suppress

from app.application.control_plane_service import ControlPlaneService
from app.application.model_release_service import ModelReleaseService
from app.core.config import Settings
from app.infrastructure.platform_clients import GatewayPolicyClient, GovernanceQualityClient
from app.infrastructure.sqlite_repository import SqliteRepository


class AppContainer:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.repository = SqliteRepository(settings.database_path, settings.schema_path)
        self.service = ControlPlaneService(self.repository)
        self.gateway_policy = GatewayPolicyClient(settings)
        self.governance_quality = GovernanceQualityClient(settings)
        self.model_releases = ModelReleaseService(
            self.repository, settings, self.gateway_policy, self.governance_quality
        )
        self._monitor_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        await self.repository.initialize()
        self._monitor_task = asyncio.create_task(self._monitor_model_releases())

    async def stop(self) -> None:
        if self._monitor_task:
            self._monitor_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._monitor_task

    async def _monitor_model_releases(self) -> None:
        while True:
            await asyncio.sleep(self.settings.model_release_monitor_interval_seconds)
            await self.model_releases.monitor_active()
