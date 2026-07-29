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
        database_path=tmp_path / "control-plane-test.db",
        schema_path=PROJECT_ROOT / "db" / "schema.sql",
    )
    with TestClient(create_app(settings)) as test_client:
        yield test_client


@pytest.fixture
def headers() -> dict[str, str]:
    return {
        "X-Tenant-Id": "tenant-a",
        "X-User-Id": "architect@example.com",
        "X-Roles": "agent-admin",
        "X-Trace-Id": "trace-test-001",
    }


@pytest.fixture
def valid_spec() -> dict[str, object]:
    return {
        "display_name": "Customer Service Agent",
        "description": "Answers customer questions with governed tools and knowledge.",
        "graph": {
            "graph_id": "customer-service-graph",
            "entrypoint": "understand",
            "terminal_nodes": ["respond"],
            "nodes": [
                {"node_id": "understand", "kind": "llm"},
                {"node_id": "retrieve", "kind": "retrieval"},
                {"node_id": "respond", "kind": "llm"},
            ],
            "edges": [
                {"from_node": "understand", "to_node": "retrieve"},
                {"from_node": "retrieve", "to_node": "respond"},
            ],
        },
        "prompt": {
            "prompt_id": "customer-service-system",
            "system_template": "You are {{persona}}. Answer with cited evidence.",
            "variables": ["persona"],
        },
        "tools": [
            {
                "tool_name": "crm.lookup_customer",
                "version": "2.1.0",
                "risk": "read_only",
            }
        ],
        "knowledge": [
            {
                "knowledge_base": "product-manual",
                "version": "2026-07-25.3",
                "top_k": 6,
            }
        ],
        "model_policy": {
            "policy_id": "balanced-routing",
            "default_route": "general",
            "routes": [
                {
                    "route_name": "general",
                    "capability": "reasoning",
                    "models": ["model-primary", "model-fallback"],
                    "data_region": "cn",
                }
            ],
        },
        "runtime_limits": {
            "max_steps": 20,
            "max_llm_calls": 10,
            "max_tool_calls": 8,
            "max_retrieval_rounds": 3,
            "max_execution_seconds": 180,
            "max_cost_usd": 1.5,
        },
        "labels": {"owner": "agent-platform", "tier": "production"},
    }
