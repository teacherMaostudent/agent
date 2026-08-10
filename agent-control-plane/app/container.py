"""Control Plane dependency composition and lifecycle ownership."""

from __future__ import annotations

import asyncio
import os
import socket
from contextlib import suppress

from app.application.control_plane_service import ControlPlaneService
from app.application.model_release_service import ModelReleaseService
from app.core.config import Settings
from app.infrastructure.platform_clients import (
    GatewayPolicyClient,
    GovernanceQualityClient,
    ModelLabClient,
)
from app.infrastructure.postgres_repository import PostgresRepository
from app.infrastructure.sqlite_repository import SqliteRepository
from app.infrastructure.temporal_release import TemporalReleaseOrchestrator
from app.infrastructure.tool_catalog import ToolCatalogValidator


class AppContainer:
    """Build adapters once and close them in reverse dependency order."""

    def __init__(self, settings: Settings, *, build_orchestrator: bool = True) -> None:
        self.settings = settings
        self.repository = (
            PostgresRepository(
                settings.database_url,
                settings.database_schema,
                settings.postgres_schema_path,
            )
            if settings.database_backend == "postgres"
            else SqliteRepository(settings.database_path, settings.schema_path)
        )
        self.gateway_policy = GatewayPolicyClient(settings)
        self.governance_quality = GovernanceQualityClient(settings)
        self.model_lab = ModelLabClient(settings)
        self.service = ControlPlaneService(
            self.repository,
            governance=self.governance_quality,
            require_quality_gate=settings.agent_release_quality_gate_required,
            tool_catalog_validator=ToolCatalogValidator(
                settings.tool_catalog_path,
                settings.contracts_schema_dir,
                required=settings.tool_catalog_required,
            ),
        )
        self.model_releases = ModelReleaseService(
            self.repository, settings, self.gateway_policy, self.governance_quality, self.model_lab
        )
        self._monitor_task: asyncio.Task[None] | None = None
        self._controller_id = f"{socket.gethostname()}-{os.getpid()}"
        self.release_orchestrator = (
            TemporalReleaseOrchestrator(
                settings.temporal_target,
                settings.temporal_namespace,
                settings.temporal_task_queue,
            )
            if settings.temporal_enabled and build_orchestrator
            else None
        )

    async def start(self) -> None:
        """Initialize persistence before serving release-management requests."""
        await self.repository.initialize()
        if not self.settings.temporal_enabled:
            self._monitor_task = asyncio.create_task(self._monitor_model_releases())

    async def stop(self) -> None:
        """Release workflow and repository resources during process shutdown."""
        if self._monitor_task:
            self._monitor_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._monitor_task
        if self.release_orchestrator is not None:
            self.release_orchestrator.close()

    async def _monitor_model_releases(self) -> None:
        while True:
            await asyncio.sleep(self.settings.model_release_monitor_interval_seconds)
            acquired = await self.repository.acquire_lease(
                "model-route-release-monitor",
                self._controller_id,
                self.settings.model_release_monitor_interval_seconds * 3,
            )
            if acquired:
                await self.model_releases.monitor_active()
