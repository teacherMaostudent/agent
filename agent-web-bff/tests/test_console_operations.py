"""Authorization and projection tests for newly exposed high-risk Console actions."""

import asyncio

import pytest
from fastapi.testclient import TestClient

from agent_web_bff import main


@pytest.mark.parametrize("percentage", ["abc", True, -1, 101, 2.5, 10**400, -(10**400)])
def test_release_rejects_invalid_percentages_without_upstream_write(monkeypatch, percentage) -> None:
    """坏表单不能产生 500 或将小数截断成另一发布比例。"""
    calls = []

    async def upstream(*args, **kwargs):
        calls.append((args, kwargs))
        return {}

    monkeypatch.setattr(main, "_control_plane", upstream)
    response = TestClient(main.app).post(
        "/api/console/agents/fixture/releases", headers=_headers("release:create"),
        json={"confirm_agent_id": "fixture", "rollout_percentage": percentage},
    )
    assert response.status_code == 422
    assert not calls


@pytest.mark.parametrize("body", ["[]", "null", "{broken"])
def test_console_rejects_nonobject_or_malformed_json(body) -> None:
    """前端/API 的损坏请求应返回 422，保留服务可用性。"""
    response = TestClient(main.app).post(
        "/api/console/agents/fixture/versions", headers=_headers("release:version:publish"),
        content=body,
    )
    assert response.status_code == 422


def _headers(permissions: str) -> dict[str, str]:
    """Build local-development identity headers; production replaces these through OIDC."""
    return {
        "X-Tenant-Id": "tenant-a",
        "X-User-Id": "operator-a",
        "X-Permissions": permissions,
    }


def test_workspace_history_page_is_translated_to_a_bounded_runtime_offset(monkeypatch) -> None:
    """The BFF owns page-to-offset translation so the browser never supplies a raw database offset."""
    calls = []

    async def runtime(_request, method, path, params=None, **_kwargs):
        calls.append((method, path, params))
        return {"items": [], "total_items": 0, "limit": params["limit"]}

    monkeypatch.setattr(main, "_runtime", runtime)
    result = asyncio.run(main.list_workspace_runs(object(), limit=999, page=999_999))

    assert result["total_items"] == 0
    assert calls == [("GET", "/agent/runs", {"limit": 100, "offset": 125_000})]


def test_release_catalog_page_is_translated_to_control_plane_offset(monkeypatch) -> None:
    """The browser selects a page, while the BFF owns bounded offset calculation for the catalog."""
    calls = []

    async def control_plane(_request, method, path, params=None, **_kwargs):
        calls.append((method, path, params))
        return {"items": [{"agent_id": "agent-9", "revision": 3}], "total_items": 17, "limit": 8}

    monkeypatch.setattr(main, "_control_plane", control_plane)
    response = TestClient(main.app).get(
        "/api/console/agents?limit=8&page=3", headers=_headers("release:read")
    )

    assert response.status_code == 200
    assert response.json()["total_items"] == 17
    assert response.json()["items"] == [{"agent_id": "agent-9", "revision": 3, "updated_at": ""}]
    assert calls == [("GET", "/v1/agents/catalog", {"limit": 8, "offset": 16})]


def test_workspace_model_catalog_is_runtime_resolved_and_session_pinned(monkeypatch) -> None:
    """The browser receives a logical-route catalog, not a mutable Gateway provider list."""
    calls = []

    async def runtime(_request, method, path, params=None, **_kwargs):
        calls.append((method, path, params))
        return {"default_route": "qwen-plus", "items": [{"route_name": "qwen-plus"}]}

    monkeypatch.setattr(main, "_runtime", runtime)
    response = TestClient(main.app).get(
        "/api/workspace/model-routes",
        headers=_headers("rag:read"),
        params={"agent_id": "general-agent", "environment": "local", "session_id": "web_12345678"},
    )

    assert response.status_code == 200
    assert response.json()["default_route"] == "qwen-plus"
    assert calls == [
        ("GET", "/agent/model-routes", {
            "agent_id": "general-agent", "environment": "local", "session_id": "web_12345678",
        })
    ]


def test_version_catalog_projects_metadata_without_snapshot_body(monkeypatch) -> None:
    """Release pickers need a semantic version and opaque Version ID, never the executable snapshot."""
    async def control_plane(_request, method, path, **_kwargs):
        assert (method, path) == ("GET", "/v1/agents/general-agent/versions")
        return [{
            "version_id": "ver_123", "semantic_version": "1.2.3", "source_revision": 7,
            "content_hash": "abc", "change_summary": "safe update", "published_by": "admin",
            "published_at": "2026-08-28T00:00:00Z", "snapshot": {"secret": "must not project"},
        }]

    monkeypatch.setattr(main, "_control_plane", control_plane)
    response = TestClient(main.app).get(
        "/api/console/agents/general-agent/versions", headers=_headers("release:read")
    )

    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["semantic_version"] == "1.2.3"
    assert item["version_id"] == "ver_123"
    assert "snapshot" not in item


def test_review_assignment_accepts_only_enabled_reviewer_in_current_tenant(monkeypatch) -> None:
    """The browser submits an opaque subject, but BFF still validates IdP tenancy and review scope."""
    runtime_calls = []

    async def identity_admin(method, path, **_kwargs):
        if path == "/users":
            return [
                {"id": "reviewer-1", "username": "expert", "enabled": True,
                 "attributes": {"tenant_id": ["tenant-a"], "permissions": ["agent:review"]}},
                {"id": "disabled-reviewer", "username": "old", "enabled": False,
                 "attributes": {"tenant_id": ["tenant-a"], "permissions": ["agent:review"]}},
                {"id": "other-tenant", "username": "other", "enabled": True,
                 "attributes": {"tenant_id": ["tenant-b"], "permissions": ["agent:review"]}},
            ]
        if path.endswith("/role-mappings/realm"):
            return [{"name": "agent-reviewer"}]
        raise AssertionError((method, path))

    async def runtime(_request, method, path, **kwargs):
        runtime_calls.append((method, path, kwargs.get("json")))
        return {}

    monkeypatch.setattr(main, "_identity_admin", identity_admin)
    monkeypatch.setattr(main, "_runtime", runtime)
    client = TestClient(main.app)
    headers = _headers("run:review:assign")

    directory = client.get("/api/workspace/reviewers", headers=headers)
    accepted = client.post(
        "/api/workspace/runs/run-1/review-assignment", headers=headers,
        json={"reviewer_id": "reviewer-1", "reason": "需要证据复核"},
    )
    rejected = client.post(
        "/api/workspace/runs/run-1/review-assignment", headers=headers,
        json={"reviewer_id": "other-tenant", "reason": "不能跨租户指派"},
    )

    assert directory.json() == {"items": [{"user_id": "reviewer-1", "username": "expert"}]}
    assert accepted.status_code == 204
    assert rejected.status_code == 422
    assert runtime_calls == [
        ("POST", "/agent/runs/run-1/review-assignments", {"reviewer_id": "reviewer-1", "reason": "需要证据复核"})
    ]


@pytest.mark.parametrize("value", [None, "tenant-a", [42]])
def test_release_rejects_non_string_allowlist(monkeypatch, value) -> None:
    """白名单不能由字符串或非字符串元素隐式构造，错误请求不写下游。"""
    calls = []

    async def upstream(*args, **kwargs):
        calls.append((args, kwargs))
        return {}

    monkeypatch.setattr(main, "_control_plane", upstream)
    response = TestClient(main.app).post(
        "/api/console/agents/fixture/releases", headers=_headers("release:create"),
        json={"confirm_agent_id": "fixture", "tenant_allowlist": value},
    )
    assert response.status_code == 422
    assert not calls


@pytest.mark.parametrize("value", [None, [], "bad"])
def test_workspace_rejects_invalid_metadata(monkeypatch, value) -> None:
    """嵌套字段损坏返回 422，不能在合并通道元数据时崩溃。"""
    calls = []

    async def upstream(*args, **kwargs):
        calls.append((args, kwargs))
        return {}

    monkeypatch.setattr(main, "_runtime", upstream)
    response = TestClient(main.app).post(
        "/api/workspace/runs", headers=_headers("rag:read"),
        json={"task": "fixture", "metadata": value},
    )
    assert response.status_code == 422
    assert not calls


def test_quota_update_preserves_other_policy_fields(monkeypatch) -> None:
    """Editing one quota must not accidentally erase the tenant's release policy."""
    calls: list[tuple[str, str, dict | None]] = []
    current = {
        "allowed_models": ["deepseek-chat"],
        "allowed_data_regions": ["cn"],
        "max_canary_percentage": 20,
        "require_approval_for_high_risk_tools": True,
        "llm_quotas": {"*": {"daily_token_limit": 100, "daily_cost_limit_usd": 1}},
    }

    async def control_plane(_request, method, path, json=None):
        calls.append((method, path, json))
        if method == "GET":
            return current
        return {**current, "llm_quotas": json["llm_quotas"]}

    monkeypatch.setattr(main, "_control_plane", control_plane)
    response = TestClient(main.app).put(
        "/api/console/llm-quotas/user-a",
        headers=_headers("quota:write"),
        json={
            "confirmSubject": "user-a",
            "dailyTokenLimit": 20_000,
            "dailyCostLimitUsd": 3.5,
        },
    )

    assert response.status_code == 200
    update = calls[-1][2]
    assert update["allowed_models"] == ["deepseek-chat"]
    assert update["max_canary_percentage"] == 20
    assert update["llm_quotas"]["user-a"]["daily_token_limit"] == 20_000


def test_worm_export_requires_current_tenant_confirmation(monkeypatch) -> None:
    """A Console operator cannot typo or substitute the export tenant in a high-risk action."""
    invoked = False

    async def governance(*_args, **_kwargs):
        nonlocal invoked
        invoked = True
        return {"job_id": "job-1", "status": "QUEUED"}

    monkeypatch.setattr(main, "_governance", governance)
    response = TestClient(main.app).post(
        "/api/console/audit-exports",
        headers=_headers("audit:export"),
        json={"confirmTenantId": "other-tenant"},
    )

    assert response.status_code == 422
    assert invoked is False


def test_identity_user_update_accepts_only_catalog_dropdown_values(monkeypatch) -> None:
    """The Console submits selected option values; unknown strings must fail before the IdP call."""
    calls: list[tuple[str, str]] = []

    async def identity_admin(method, path, **_kwargs):
        calls.append((method, path))
        if method == "GET" and path.endswith("/role-mappings/realm"):
            return [{"id": "role-user", "name": "agent-user"}]
        if method == "GET" and path == "/roles":
            return [{"id": "role-user", "name": "agent-user"}]
        if method == "GET":
            return {
                "id": "user-1", "username": "managed-user", "enabled": True,
                "attributes": {"tenant_id": ["tenant-a"], "permissions": ["rag:read"]},
            }
        return {}

    monkeypatch.setattr(main, "_identity_admin", identity_admin)

    async def active_tenant(_request, tenant_id: str) -> None:
        """The IdP mutation test isolates catalog lookups; catalog validation has its own tests."""
        assert tenant_id == "tenant-a"

    monkeypatch.setattr(main, "_require_active_catalog_tenant", active_tenant)
    client = TestClient(main.app)
    allowed = client.put(
        "/api/console/identity/users/user-1",
        headers=_headers("identity:users:write"),
        json={
            "tenant_id": "tenant-a",
            "enabled": True,
            "roles": ["agent-user"],
            "permissions": ["rag:read", "run:tenant:read"],
            "reason": "assign reviewed access",
        },
    )
    rejected = client.put(
        "/api/console/identity/users/user-1",
        headers=_headers("identity:users:write"),
        json={
            "tenant_id": "tenant-a",
            "roles": ["arbitrary-role"],
            "permissions": ["root:everything"],
            "reason": "should be rejected",
        },
    )

    assert allowed.status_code == 200
    assert ("PUT", "/users/user-1") in calls
    assert rejected.status_code == 422
