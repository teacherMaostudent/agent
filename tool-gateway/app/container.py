from __future__ import annotations

import asyncio
from contextlib import suppress

from app.application import ToolExecutionService
from app.core.config import Settings, get_settings
from app.infrastructure.repository import SqliteRepository
from app.governance import GovernanceOutboxPublisher
from app.registry import ToolRegistry, load_registry


class Container:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        registry: ToolRegistry | None = None,
        repository: SqliteRepository | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.settings.ensure_directories()
        self.repository = repository or SqliteRepository(self.settings.database_path)
        self.registry = registry or load_registry(
            self.settings.tools_config_path,
            allow_private_networks=self.settings.allow_private_networks,
            max_response_bytes=self.settings.max_response_bytes,
        )
        self.governance = GovernanceOutboxPublisher(
            self.repository, self.settings.governance_base_url, self.settings.governance_event_key,
            self.settings.http_connect_timeout,
        )
        self.execution = ToolExecutionService(
            self.registry,
            self.repository,
            approval_ttl_seconds=self.settings.approval_ttl_seconds,
            idempotency_ttl_seconds=self.settings.idempotency_ttl_seconds,
            event_publisher=self.governance.publish_invocation,
        )

    def ready(self) -> dict:
        self.repository.ping()
        return {"registered_tools": self.registry.count, "persistence": "sqlite"}

    def close(self) -> None:
        adapters = list(self.registry.adapters())
        if adapters:
            with suppress(RuntimeError):
                asyncio.run(_close_all(adapters))
        self.repository.close()


async def _close_all(adapters) -> None:
    await asyncio.gather(*(adapter.close() for adapter in adapters))
