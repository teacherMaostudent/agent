"""Agent Lab 的依赖装配：API 与 Temporal Worker 使用同一服务契约但独立进程运行。"""

from __future__ import annotations

from platform_infra.identity import build_workload_token_provider
from platform_infra.mtls import mtls_httpx_options

from app.clients import ControlPlaneClient, GovernanceClient, RuntimeClient
from app.main_settings import Settings
from app.repository import ExperimentRepository, PostgresExperimentRepository
from app.service import AgentLabService
from app.temporal_queue import LocalExperimentQueue, TemporalExperimentQueue
from app.worker import AgentLabWorker


class AgentLabContainer:
    """集中创建数据库、身份客户端、队列和 Worker，禁止 API 路由自行拥有基础设施。"""

    def __init__(self, settings: Settings, *, build_queue: bool = True) -> None:
        """按环境选择本地或生产适配器；生产校验已在 Settings 阶段 fail-closed。"""
        self.settings = settings
        self.repository = (
            PostgresExperimentRepository(settings.database_url, settings.database_schema)
            if settings.database_backend == "postgres"
            else ExperimentRepository(settings.database_path)
        )
        if settings.database_backend == "postgres":
            self.repository.initialize()
        workload_identity = build_workload_token_provider(settings)
        mtls_options = mtls_httpx_options(
            enabled=settings.mtls_enabled,
            ca_file=settings.mtls_ca_file,
            cert_file=settings.mtls_cert_file,
            key_file=settings.mtls_key_file,
        )
        self.service = AgentLabService(
            self.repository,
            ControlPlaneClient(
                settings.control_plane_base_url,
                settings.control_plane_runtime_key,
                settings.request_timeout_seconds,
                workload_identity,
                mtls_options,
            ),
            RuntimeClient(
                settings.runtime_base_url,
                settings.request_timeout_seconds,
                workload_identity,
                mtls_options,
            ),
            GovernanceClient(
                settings.governance_base_url,
                settings.governance_auditor_key,
                settings.request_timeout_seconds,
                workload_identity,
                mtls_options,
            ),
            settings.max_cases,
        )
        self.worker = AgentLabWorker(
            self.repository,
            self.service,
            worker_id=settings.worker_id,
            lease_seconds=settings.job_lease_seconds,
            retry_initial_seconds=settings.retry_initial_seconds,
            retry_max_seconds=settings.retry_max_seconds,
        )
        self.queue = (
            TemporalExperimentQueue(
                settings.temporal_target,
                settings.temporal_namespace,
                settings.temporal_task_queue,
            )
            if build_queue and settings.temporal_enabled
            else LocalExperimentQueue(self.worker.execute)
        )

    def close(self) -> None:
        """依赖关闭遵循队列先停、再释放存储的次序，避免后台活动使用已关闭连接。"""
        self.queue.close()
        self.repository.close()
