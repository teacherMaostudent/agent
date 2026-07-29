from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    settings = Settings(
        database_path=tmp_path / "governance-test.db",
        schema_path=PROJECT_ROOT / "db" / "schema.sql",
    )
    with TestClient(create_app(settings)) as test_client:
        yield test_client


@pytest.fixture
def auditor_headers() -> dict[str, str]:
    return {
        "X-Tenant-Id": "tenant-a",
        "X-User-Id": "auditor@example.com",
        "X-Roles": "governance-auditor",
    }


def event(
    event_id: str, event_type: str, payload: dict[str, object], tenant_id: str = "tenant-a"
) -> dict[str, object]:
    return {
        "event_id": event_id,
        "source_service": "tool-gateway",
        "event_type": event_type,
        "trace_id": "trace-governance-test",
        "tenant_id": tenant_id,
        "occurred_at": "2026-07-25T00:00:00Z",
        "payload": payload,
    }
