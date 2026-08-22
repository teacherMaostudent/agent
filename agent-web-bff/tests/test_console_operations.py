"""Authorization and projection tests for newly exposed high-risk Console actions."""

from fastapi.testclient import TestClient

from agent_web_bff import main


def _headers(permissions: str) -> dict[str, str]:
    """Build local-development identity headers; production replaces these through OIDC."""
    return {
        "X-Tenant-Id": "tenant-a",
        "X-User-Id": "operator-a",
        "X-Permissions": permissions,
    }


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
