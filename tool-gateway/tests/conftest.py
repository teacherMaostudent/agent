from __future__ import annotations

from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.container import Container
from app.core.config import Settings
from app.domain.models import HttpTransport, ToolRisk, ToolSpec
from app.infrastructure.adapters import CallableToolAdapter
from app.infrastructure.repository import SqliteRepository
from app.main import create_app
from app.registry import ToolRegistry


def tool_spec(
    name: str,
    *,
    version: str = "1.0.0",
    permission: str = "tool:read",
    risk: ToolRisk = ToolRisk.READ_ONLY,
    approval_required: bool = False,
    enabled_tenants: list[str] | None = None,
    idempotent: bool = True,
    timeout_seconds: float = 1,
    retry_attempts: int = 1,
    breaker_failure_threshold: int = 3,
    input_schema: dict[str, Any] | None = None,
    output_schema: dict[str, Any] | None = None,
) -> ToolSpec:
    return ToolSpec(
        name=name,
        version=version,
        description=f"Test tool {name}",
        input_schema=input_schema
        or {
            "type": "object",
            "properties": {"value": {"type": "string", "minLength": 2}},
            "required": ["value"],
            "additionalProperties": False,
        },
        output_schema=output_schema or {"type": "object"},
        required_permissions=[permission],
        risk=risk,
        approval_required=approval_required,
        enabled_tenants=enabled_tenants or ["*"],
        idempotent=idempotent,
        timeout_seconds=timeout_seconds,
        retry_attempts=retry_attempts,
        rate_limit_per_minute=100,
        breaker_failure_threshold=breaker_failure_threshold,
        breaker_reset_seconds=60,
        transport=HttpTransport(
            url="https://tools.example.com/invoke",
            allowed_hosts=["tools.example.com"],
        ),
    )


@pytest.fixture
def gateway_factory(tmp_path: Path):
    containers: list[Container] = []

    def factory(
        registrations: list[tuple[ToolSpec, Callable[[dict[str, Any], Any], Any]]],
    ) -> TestClient:
        registry = ToolRegistry()
        for spec, handler in registrations:
            registry.register(spec, CallableToolAdapter(handler))
        settings = Settings(
            database_path=tmp_path / f"gateway-{len(containers)}.db",
            tools_config_path=tmp_path / "unused.json",
            require_service_auth=True,
            service_api_key="service-secret",
            admin_api_key="admin-secret",
            allow_private_networks=False,
        )
        container = Container(
            settings,
            registry=registry,
            repository=SqliteRepository(settings.database_path),
        )
        containers.append(container)
        return TestClient(create_app(container))

    yield factory

    for container in containers:
        with suppress(Exception):
            container.close()


@pytest.fixture
def trusted_headers() -> dict[str, str]:
    return {
        "X-Tool-Gateway-Key": "service-secret",
        "X-Tenant-Id": "tenant-a",
        "X-User-Id": "user-a",
        "X-Permissions": "tool:read,tool:write,tool:approve",
        "X-Request-Id": "request-a",
    }
