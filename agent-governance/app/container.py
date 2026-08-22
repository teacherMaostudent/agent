"""Governance dependency composition and repository lifecycle wiring."""

from __future__ import annotations

from platform_infra.schema_registry import SchemaRegistry

from app.application.compliance_service import ComplianceService
from app.application.evaluation_service import EvaluationService
from app.application.governance_service import GovernanceService
from app.application.worm_export_service import WormExportService
from app.core.config import Settings
from app.infrastructure.llm_gateway_client import LlmGatewayClient
from app.infrastructure.postgres_repository import PostgresRepository
from app.infrastructure.sqlite_repository import SqliteRepository


class AppContainer:
    """Construct Governance services with one shared persistence boundary."""

    def __init__(self, settings: Settings) -> None:
        """装配审计、评测、合规服务及其共享持久化和模型调用边界。"""
        self.settings = settings
        self.schema_registry = SchemaRegistry(settings.contracts_schema_dir)
        self.repository = (
            PostgresRepository(
                settings.database_url,
                settings.database_schema,
                settings.postgres_schema_path,
            )
            if settings.database_backend == "postgres"
            else SqliteRepository(settings.database_path, settings.schema_path)
        )
        self.service = GovernanceService(self.repository)
        self.llm_gateway = LlmGatewayClient(settings)
        self.evaluation = EvaluationService(self.repository, settings, self.llm_gateway)
        self.compliance = ComplianceService(
            self.repository, self.llm_gateway, settings.judge_primary_model
        )
        self.worm_exports = WormExportService(self.repository, settings)

    async def start(self) -> None:
        """在接收审计或评测请求前初始化唯一持久化边界。"""
        await self.repository.initialize()
