from __future__ import annotations

from copy import deepcopy

from fastapi.testclient import TestClient


def _create_agent(
    client: TestClient,
    headers: dict[str, str],
    valid_spec: dict[str, object],
) -> dict[str, object]:
    response = client.post(
        "/v1/agents",
        headers=headers,
        json={"agent_id": "customer-service", "spec": valid_spec},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _publish(
    client: TestClient,
    headers: dict[str, str],
    semantic_version: str,
) -> dict[str, object]:
    response = client.post(
        "/v1/agents/customer-service/versions",
        headers=headers,
        json={"semantic_version": semantic_version, "change_summary": "tested release"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _release(
    client: TestClient,
    headers: dict[str, str],
    version_id: str,
    rollout_percentage: int,
    tenant_allowlist: list[str] | None = None,
) -> dict[str, object]:
    response = client.post(
        "/v1/agents/customer-service/releases",
        headers=headers,
        json={
            "version_id": version_id,
            "environment": "production",
            "rollout_percentage": rollout_percentage,
            "tenant_allowlist": tenant_allowlist or [],
            "reason": "automated test",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_publish_snapshot_is_immutable_and_tenant_isolated(
    client: TestClient,
    headers: dict[str, str],
    valid_spec: dict[str, object],
) -> None:
    _create_agent(client, headers, valid_spec)
    validation = client.post(
        "/v1/agents/customer-service/validate",
        headers=headers,
    )
    assert validation.status_code == 200
    assert validation.json() == {"valid": True, "issues": []}

    version = _publish(client, headers, "1.0.0")
    assert version["snapshot"]["agent_version"] == "customer-service:1.0.0"
    assert version["snapshot"]["knowledge_version"].startswith("kb:")

    changed_spec = deepcopy(valid_spec)
    changed_spec["display_name"] = "Customer Service Agent vNext"
    update = client.put(
        "/v1/agents/customer-service/draft",
        headers=headers,
        json={"expected_revision": 1, "spec": changed_spec},
    )
    assert update.status_code == 200
    assert update.json()["revision"] == 2

    stored_version = client.get(
        f"/v1/agents/customer-service/versions/{version['version_id']}",
        headers=headers,
    )
    assert stored_version.status_code == 200
    assert stored_version.json()["snapshot"]["spec"]["display_name"] == "Customer Service Agent"

    other_tenant_headers = {
        **headers,
        "X-Tenant-Id": "tenant-b",
        "X-User-Id": "other@example.com",
    }
    isolated = client.get(
        "/v1/agents/customer-service",
        headers=other_tenant_headers,
    )
    assert isolated.status_code == 404


def test_optimistic_concurrency_rejects_stale_draft(
    client: TestClient,
    headers: dict[str, str],
    valid_spec: dict[str, object],
) -> None:
    _create_agent(client, headers, valid_spec)
    first = client.put(
        "/v1/agents/customer-service/draft",
        headers=headers,
        json={"expected_revision": 1, "spec": valid_spec},
    )
    assert first.status_code == 200

    stale = client.put(
        "/v1/agents/customer-service/draft",
        headers=headers,
        json={"expected_revision": 1, "spec": valid_spec},
    )
    assert stale.status_code == 409
    assert stale.json()["code"] == "conflict"
    assert stale.json()["details"]["current_revision"] == 2


def test_invalid_draft_cannot_be_published(
    client: TestClient,
    headers: dict[str, str],
    valid_spec: dict[str, object],
) -> None:
    invalid = deepcopy(valid_spec)
    invalid["tools"] = [
        {
            "tool_name": "payment.refund",
            "version": "1.0.0",
            "risk": "write_high_risk",
            "approval_required": False,
        }
    ]
    invalid["prompt"]["variables"] = []
    _create_agent(client, headers, invalid)

    validation = client.post(
        "/v1/agents/customer-service/validate",
        headers=headers,
    )
    assert validation.status_code == 200
    report = validation.json()
    assert report["valid"] is False
    assert {item["code"] for item in report["issues"]} >= {
        "prompt.undeclared_variables",
        "tools.approval_required",
    }

    publish = client.post(
        "/v1/agents/customer-service/versions",
        headers=headers,
        json={"semantic_version": "1.0.0"},
    )
    assert publish.status_code == 422
    assert publish.json()["code"] == "draft_validation_failed"


def test_canary_session_is_pinned_then_reassigned_after_rollback(
    client: TestClient,
    headers: dict[str, str],
    valid_spec: dict[str, object],
) -> None:
    _create_agent(client, headers, valid_spec)
    stable_version = _publish(client, headers, "1.0.0")
    stable_release = _release(client, headers, stable_version["version_id"], 5)
    assert stable_release["rollout_percentage"] == 100
    assert stable_release["previous_release_id"] is None

    changed_spec = deepcopy(valid_spec)
    changed_spec["description"] = "Canary version"
    update = client.put(
        "/v1/agents/customer-service/draft",
        headers=headers,
        json={"expected_revision": 1, "spec": changed_spec},
    )
    assert update.status_code == 200
    canary_version = _publish(client, headers, "1.1.0")
    canary_release = _release(
        client,
        headers,
        canary_version["version_id"],
        20,
        tenant_allowlist=["tenant-a"],
    )

    first_resolve = client.get(
        "/v1/runtime/agents/customer-service/resolve",
        headers=headers,
        params={"environment": "production", "session_id": "session-canary"},
    )
    assert first_resolve.status_code == 200, first_resolve.text
    assert first_resolve.json()["release_id"] == canary_release["release_id"]
    assert first_resolve.json()["assignment"] == "allowlist"
    assert first_resolve.json()["pinned"] is False

    second_resolve = client.get(
        "/v1/runtime/agents/customer-service/resolve",
        headers=headers,
        params={"environment": "production", "session_id": "session-canary"},
    )
    assert second_resolve.status_code == 200
    assert second_resolve.json()["release_id"] == canary_release["release_id"]
    assert second_resolve.json()["assignment"] == "pinned"
    assert second_resolve.json()["pinned"] is True

    rollback = client.post(
        f"/v1/releases/{canary_release['release_id']}/rollback",
        headers=headers,
    )
    assert rollback.status_code == 200
    assert rollback.json()["status"] == "rolled_back"

    after_rollback = client.get(
        "/v1/runtime/agents/customer-service/resolve",
        headers=headers,
        params={"environment": "production", "session_id": "session-canary"},
    )
    assert after_rollback.status_code == 200
    assert after_rollback.json()["release_id"] == stable_release["release_id"]
    assert after_rollback.json()["pinned"] is False

    outbox = client.get("/v1/outbox", headers=headers)
    assert outbox.status_code == 200
    event_types = {item["event_type"] for item in outbox.json()["items"]}
    assert {
        "AgentCreated",
        "AgentVersionPublished",
        "ReleaseActivated",
        "ReleaseCanaryStarted",
        "ReleaseRolledBack",
    }.issubset(event_types)


def test_tenant_policy_blocks_unapproved_model(
    client: TestClient,
    headers: dict[str, str],
    valid_spec: dict[str, object],
) -> None:
    policy = client.put(
        "/v1/tenant-policy",
        headers=headers,
        json={
            "allowed_models": ["approved-model"],
            "allowed_data_regions": ["cn"],
            "max_canary_percentage": 50,
            "require_approval_for_high_risk_tools": True,
        },
    )
    assert policy.status_code == 200
    _create_agent(client, headers, valid_spec)

    validation = client.post(
        "/v1/agents/customer-service/validate",
        headers=headers,
    )
    assert validation.status_code == 200
    assert validation.json()["valid"] is False
    assert "policy.model_not_allowed" in {issue["code"] for issue in validation.json()["issues"]}


def test_agent_release_records_governance_quality_gate(
    client: TestClient,
    headers: dict[str, str],
    valid_spec: dict[str, object],
) -> None:
    _create_agent(client, headers, valid_spec)
    version = _publish(client, headers, "3.0.0")

    async def passed_gate(tenant_id: str, run_id: str) -> dict:
        assert tenant_id == "tenant-a"
        assert run_id == "judge-run-3"
        return {
            "id": "gate-3",
            "passed": True,
            "metrics": {"averageScore": 94, "passRate": 1.0},
            "reasons": [],
        }

    client.app.state.container.governance_quality.quality_gate = passed_gate
    response = client.post(
        "/v1/agents/customer-service/releases",
        headers=headers,
        json={
            "version_id": version["version_id"],
            "environment": "production",
            "rollout_percentage": 100,
            "reason": "quality gated",
            "quality_gate_run_id": "judge-run-3",
        },
    )

    assert response.status_code == 201, response.text
    assert response.json()["quality_gate_id"] == "gate-3"
    assert response.json()["quality_gate_metrics"]["averageScore"] == 94
