"""Tool asset lifecycle: drafts are mutable, runtime releases are reviewed immutable snapshots."""

from fastapi.testclient import TestClient


def _definition() -> dict[str, object]:
    """提供完整的最小执行契约，避免测试绕过生产 Schema 边界。"""
    return {
        "name": "tenant_lookup",
        "version": "1.0.0",
        "description": "read a tenant record",
        "input_schema": {"type": "object"},
        "output_schema": {"type": "object"},
        "transport": {"kind": "http", "url": "http://example.invalid/lookup"},
        "required_permissions": ["tenant:read"],
        "risk": "read_only",
    }


def test_tool_draft_review_release_and_retirement_are_fail_closed(
    client: TestClient, headers: dict[str, str]
) -> None:
    """验证未审核版本不能发布，退役后不能再进入 Gateway 投影。"""
    created = client.post(
        "/v1/tools",
        headers=headers,
        json={
            "tool_id": "tenant_lookup",
            "definition": _definition(),
            "owner_team": "core",
        },
    )
    assert created.status_code == 201, created.text
    assert created.json()["revision"] == 1

    candidate = client.post(
        "/v1/tools/tenant_lookup/versions",
        headers=headers,
        json={
            "semantic_version": "1.0.0",
            "change_summary": "initial contract",
        },
    )
    assert candidate.status_code == 201, candidate.text
    version_id = candidate.json()["version_id"]
    assert candidate.json()["status"] == "candidate"

    denied = client.post(f"/v1/tools/tenant_lookup/versions/{version_id}/release", headers=headers)
    assert denied.status_code == 409

    approved = client.post(
        f"/v1/tools/tenant_lookup/versions/{version_id}/review",
        headers=headers,
        json={
            "decision": "approve",
            "comment": "contract reviewed",
        },
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "approved"

    released = client.post(
        f"/v1/tools/tenant_lookup/versions/{version_id}/release", headers=headers
    )
    assert released.status_code == 200, released.text
    assert released.json()["status"] == "published"
    projection = client.get(
        "/internal/v1/tool-catalog/runtime-projection",
        headers={
            "X-Tenant-Id": "tenant-a",
            "X-User-Id": "gateway",
            "X-Runtime-Key": "",
        },
    )
    # Test mode does not require a runtime key, but it still exercises the internal boundary.
    assert projection.status_code == 200, projection.text
    assert [item["name"] for item in projection.json()["catalog"]["tools"]] == ["tenant_lookup"]

    retired = client.post(
        f"/v1/tools/tenant_lookup/versions/{version_id}/status",
        headers=headers,
        json={
            "status": "retired",
            "reason": "adapter removed",
        },
    )
    assert retired.status_code == 200, retired.text
    assert (
        client.get(
            "/internal/v1/tool-catalog/runtime-projection",
            headers={
                "X-Tenant-Id": "tenant-a",
                "X-User-Id": "gateway",
                "X-Runtime-Key": "",
            },
        ).json()["catalog"]["tools"]
        == []
    )
