from __future__ import annotations

from app.application.compliance_service import ComplianceService
from app.application.evaluation_service import EvaluationService
from app.application.governance_service import GovernanceService
from app.core.config import Settings
from app.infrastructure.llm_gateway_client import LlmGatewayClient
from app.infrastructure.sqlite_repository import SqliteRepository


class AppContainer:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.repository = SqliteRepository(settings.database_path, settings.schema_path)
        self.service = GovernanceService(self.repository)
        self.llm_gateway = LlmGatewayClient(settings)
        self.evaluation = EvaluationService(self.repository, settings, self.llm_gateway)
        self.compliance = ComplianceService(
            self.repository, self.llm_gateway, settings.judge_primary_model
        )

    async def start(self) -> None:
        await self.repository.initialize()
