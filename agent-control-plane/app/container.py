"""Control Plane dependency composition and lifecycle ownership."""

from __future__ import annotations

import asyncio
import json
import os
import socket
from contextlib import suppress

from app.application.control_plane_service import ControlPlaneService
from app.application.model_release_service import ModelReleaseService
from app.core.config import Settings
from app.domain.models import (
    Identity,
    Tenant,
    ToolDraftCreate,
    ToolReviewCreate,
    ToolVersionPublish,
    utc_now,
)
from app.infrastructure.platform_clients import (
    AgentLabClient,
    GatewayPolicyClient,
    GovernanceQualityClient,
    ModelLabClient,
)
from app.infrastructure.postgres_repository import PostgresRepository
from app.infrastructure.runtime_executor_catalog import RuntimeExecutorCatalog
from app.infrastructure.sqlite_repository import SqliteRepository
from app.infrastructure.temporal_release import TemporalReleaseOrchestrator
from app.infrastructure.tool_catalog import ToolCatalogValidator
from app.infrastructure.tool_runtime_projection import ToolRuntimeProjectionPublisher


class AppContainer:
    """Build adapters once and close them in reverse dependency order."""

    def __init__(self, settings: Settings, *, build_orchestrator: bool = True) -> None:
        """按配置装配仓储、外部治理客户端与发布编排器，集中管理其生命周期。"""
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
        self.agent_lab = AgentLabClient(settings)
        self.service = ControlPlaneService(
            self.repository,
            governance=self.governance_quality,
            require_quality_gate=settings.agent_release_quality_gate_required,
            require_knowledge_contracts=settings.agent_release_knowledge_contract_required,
            agent_lab=self.agent_lab,
            require_agent_lab=settings.agent_lab_required,
            tool_catalog_validator=ToolCatalogValidator(
                settings.tool_catalog_path,
                settings.contracts_schema_dir,
                required=settings.tool_catalog_required,
            ),
            runtime_executor_catalog=RuntimeExecutorCatalog(
                settings.runtime_executor_catalog_path,
                required=settings.runtime_executor_catalog_required,
                timeout=settings.runtime_executor_catalog_timeout_seconds,
                service_key=settings.runtime_executor_catalog_service_api_key,
            ),
            gateway_policy=self.gateway_policy if settings.llm_quota_sync_enabled else None,
        )
        # Published projection is Gateway input; Draft and review data cannot cross this boundary.
        self.tool_runtime_projection = ToolRuntimeProjectionPublisher(
            self.repository, settings.contracts_schema_dir
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
        """按依赖顺序初始化仓储、远程客户端和后台编排器；任一必需依赖失败都会阻止服务进入就
        绪。

        Initialize persistence before serving release-management requests.
        """
        await self.repository.initialize()
        if self.settings.bootstrap_tenant_id:
            # Explicit local bootstrap: idempotent and incapable of overwriting an administrator's
            # later catalog update. It is intentionally unset in production configuration.
            now = utc_now()
            await self.repository.ensure_tenant(
                Tenant(
                    tenant_id=self.settings.bootstrap_tenant_id,
                    display_name=self.settings.bootstrap_tenant_display_name,
                    data_region=self.settings.bootstrap_tenant_data_region,
                    created_by="bootstrap",
                    created_at=now,
                    updated_by="bootstrap",
                    updated_at=now,
                )
            )
            await self._bootstrap_tool_assets()
        if not self.settings.temporal_enabled:
            self._monitor_task = asyncio.create_task(self._monitor_model_releases())

    async def _bootstrap_tool_assets(self) -> None:
        """仅为显式本地 demo 租户导入历史 JSON 为正式 Tool Release。

        该迁移幂等且不会覆盖管理员维护的 Draft/Version；生产环境未设置
        bootstrap_tenant_id，因此不会从文件隐式创建工具资产。
        """
        path = self.settings.tool_catalog_path
        if not path.exists():
            return
        identity = Identity(
            tenant_id=self.settings.bootstrap_tenant_id,
            user_id="bootstrap",
            roles=["agent-admin"],
        )
        for definition in json.loads(path.read_text(encoding="utf-8")).get("tools", []):
            tool_id = str(definition.get("name", ""))
            if not tool_id or await self.repository.get_tool(identity.tenant_id, tool_id):
                continue
            try:
                await self.service.create_tool(
                    identity,
                    ToolDraftCreate(tool_id=tool_id, definition=definition, owner_team="bootstrap"),
                    "bootstrap-tool-assets",
                )
                version = await self.service.publish_tool_version(
                    identity,
                    tool_id,
                    ToolVersionPublish(semantic_version=str(definition["version"]), change_summary="local seed"),
                    "bootstrap-tool-assets",
                )
                await self.service.review_tool_version(
                    identity, tool_id, version.version_id,
                    ToolReviewCreate(decision="approve", comment="local bootstrap seed"),
                    "bootstrap-tool-assets",
                )
                await self.service.release_tool_version(
                    identity, tool_id, version.version_id, "bootstrap-tool-assets"
                )
            except Exception:
                # Startup must not mask a malformed explicit local seed; fail ready instead.
                raise

    async def stop(self) -> None:
        """按逆序停止后台任务并关闭连接池；关闭过程不推进任何业务状态。

        Release workflow and repository resources during process shutdown.
        """
        if self._monitor_task:
            self._monitor_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._monitor_task
        if self.release_orchestrator is not None:
            self.release_orchestrator.close()

    async def _monitor_model_releases(self) -> None:
        """非 Temporal 模式下由持久化租约选主监控，避免多副本重复推进灰度。"""
        while True:
            await asyncio.sleep(self.settings.model_release_monitor_interval_seconds)
            acquired = await self.repository.acquire_lease(
                "model-route-release-monitor",
                self._controller_id,
                self.settings.model_release_monitor_interval_seconds * 3,
            )
            if acquired:
                await self.model_releases.monitor_active()
