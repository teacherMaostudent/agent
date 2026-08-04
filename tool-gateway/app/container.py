"""Tool Gateway composition root for catalog, execution and audit adapters."""

from __future__ import annotations

import asyncio
from contextlib import suppress

from platform_infra.identity import build_workload_token_provider
from platform_infra.mtls import mtls_httpx_options
from platform_infra.opa import OpaAuthorizer

from app.application import ToolExecutionService
from app.core.config import Settings, get_settings
from app.governance import GovernanceOutboxPublisher
from app.infrastructure.postgres_repository import PostgresRepository
from app.infrastructure.repository import SqliteRepository
from app.registry import ToolRegistry, load_registry
from app.resilience import RedisFixedWindowRateLimiter


class Container:
    """Own shared HTTP/database resources and validate readiness dependencies."""
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        registry: ToolRegistry | None = None,
        repository: SqliteRepository | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.settings.ensure_directories()
        self.repository = repository or (
            PostgresRepository(self.settings.database_url, self.settings.database_schema)
            if self.settings.database_backend == "postgres"
            else SqliteRepository(self.settings.database_path)
        )
        self.registry = registry or load_registry(
            self.settings.tools_config_path,
            allow_private_networks=self.settings.allow_private_networks,
            max_response_bytes=self.settings.max_response_bytes,
            schema_dir=self.settings.contracts_schema_dir,
            client_options=mtls_httpx_options(
                enabled=self.settings.mtls_enabled,
                ca_file=self.settings.mtls_ca_file,
                cert_file=self.settings.mtls_cert_file,
                key_file=self.settings.mtls_key_file,
            ),
        )
        self.governance = GovernanceOutboxPublisher(
            self.repository,
            self.settings.governance_base_url,
            self.settings.governance_event_key,
            self.settings.http_connect_timeout,
            build_workload_token_provider(self.settings),
        )
        self.execution = ToolExecutionService(
            self.registry,
            self.repository,
            approval_ttl_seconds=self.settings.approval_ttl_seconds,
            idempotency_ttl_seconds=self.settings.idempotency_ttl_seconds,
            event_publisher=self.governance.publish_invocation,
            rate_limiter=(
                RedisFixedWindowRateLimiter(self.settings.redis_url)
                if self.settings.redis_url
                else None
            ),
            policy_authorizer=(
                OpaAuthorizer(self.settings.opa_base_url, self.settings.opa_decision_path)
                if self.settings.opa_enabled
                else None
            ),
        )

    def ready(self) -> dict:
        self.repository.ping()
        return {
            "registered_tools": self.registry.count,
            "persistence": self.settings.database_backend,
        }

    def close(self) -> None:
        adapters = list(self.registry.adapters())
        if adapters:
            with suppress(RuntimeError):
                asyncio.run(_close_all(adapters))
        self.repository.close()


async def _close_all(adapters) -> None:
    await asyncio.gather(*(adapter.close() for adapter in adapters))
