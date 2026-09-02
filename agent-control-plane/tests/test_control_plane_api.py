from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from fastapi.testclient import TestClient

from app.infrastructure.runtime_executor_catalog import RuntimeExecutorCatalog


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


def test_agent_catalog_is_tenant_scoped_and_paginates_in_database(
    client: TestClient,
    headers: dict[str, str],
    valid_spec: dict[str, object],
) -> None:
    """发布目录页返回稳定元数据与总数，不要求 Console 拉取全量 Draft。"""
    for agent_id in ("agent-01", "agent-02", "agent-03"):
        response = client.post(
            "/v1/agents", headers=headers, json={"agent_id": agent_id, "spec": valid_spec}
        )
        assert response.status_code == 201, response.text

    second_page = client.get("/v1/agents/catalog?limit=2&offset=2", headers=headers)
    other_tenant = client.get(
        "/v1/agents/catalog?limit=2&offset=0",
        headers={**headers, "X-Tenant-Id": "tenant-b"},
    )

    assert second_page.status_code == 200
    assert second_page.json()["total_items"] == 3
    assert second_page.json()["limit"] == 2
    assert len(second_page.json()["items"]) == 1
    assert set(second_page.json()["items"][0]) == {"agent_id", "revision", "updated_at"}
    assert other_tenant.json() == {"items": [], "total_items": 0, "limit": 2, "offset": 0}


def test_release_requires_runtime_cluster_capability_when_catalog_is_enabled(
    client: TestClient,
    headers: dict[str, str],
    valid_spec: dict[str, object],
    monkeypatch,
    tmp_path: Path,
) -> None:
    """发布必须同时匹配目录和实例能力；静态 Profile 声明不能替代运行实例证明。"""
    _create_agent(client, headers, valid_spec)
    version = _publish(client, headers, "1.0.0")
    catalog_path = tmp_path / "runtime-executors.json"
    catalog_path.write_text(
        '{"version":"runtime-executor-catalog/v1","clusters":[{"cluster_id":"prod-a",'
        '"environment":"production","base_url":"http://runtime-a",'
        '"executor_profiles":["declarative-langgraph/v1"],'
        '"capabilities":["context","llm","retrieval","tool","workflow"]}]}',
        encoding="utf-8",
    )
    catalog = RuntimeExecutorCatalog(catalog_path, required=True, timeout=1, service_key="key")
    monkeypatch.setattr(
        catalog,
        "_capabilities",
        lambda _: {
            "catalog_version": "runtime-executor-catalog/v1",
            "executor_profiles": ["declarative-langgraph/v1"],
            "capabilities": ["context", "llm", "retrieval", "tool", "workflow"],
        },
    )
    client.app.state.container.service._runtime_executor_catalog = catalog

    release = _release(client, headers, str(version["version_id"]), 100)

    assert release["runtime_executor_catalog_version"] == "runtime-executor-catalog/v1"
    assert release["runtime_executor_cluster_id"] == "prod-a"


def test_runtime_catalog_rejects_plan_when_instance_lacks_required_capability(
    monkeypatch, tmp_path: Path
) -> None:
    """发布目录和实例探测都必须证明能力完整，不能只校验执行器 Profile。"""
    catalog_path = tmp_path / "runtime-executors.json"
    catalog_path.write_text(
        '{"version":"runtime-executor-catalog/v1","clusters":[{"cluster_id":"prod-a",'
        '"environment":"production","base_url":"http://runtime-a",'
        '"executor_profiles":["declarative-langgraph/v1"],'
        '"capabilities":["context","llm"]}]}',
        encoding="utf-8",
    )
    catalog = RuntimeExecutorCatalog(catalog_path, required=True, timeout=1, service_key="key")
    monkeypatch.setattr(
        catalog,
        "_capabilities",
        lambda _: {
            "catalog_version": "runtime-executor-catalog/v1",
            "executor_profiles": ["declarative-langgraph/v1"],
            "capabilities": ["context", "llm"],
        },
    )

    try:
        catalog.validate(
            "production",
            "declarative-langgraph/v1",
            required_capabilities=["context", "llm", "tool"],
        )
    except ValueError as exc:
        assert "capabilities missing" in str(exc)
    else:
        raise AssertionError("release must reject an incompletely deployed Runtime")


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
    assert version["snapshot"]["runtime_artifact"]["schema_version"] == "runtime-snapshot/v1"
    assert (
        version["snapshot"]["runtime_artifact"]["plan"]["executor_profile"]
        == "declarative-langgraph/v1"
    )

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


def test_shadow_projection_is_persisted_and_never_selected_by_normal_runtime(
    client: TestClient,
    headers: dict[str, str],
    valid_spec: dict[str, object],
) -> None:
    """Shadow 是内部镜像资格，不得因候选 Release 为 active 而泄漏给业务用户。"""
    _create_agent(client, headers, valid_spec)
    stable = _release(client, headers, _publish(client, headers, "1.0.0")["version_id"], 100)
    changed = deepcopy(valid_spec)
    changed["description"] = "candidate for shadow"
    assert client.put(
        "/v1/agents/customer-service/draft", headers=headers,
        json={"expected_revision": 1, "spec": changed},
    ).status_code == 200
    candidate = _release(
        client,
        headers,
        _publish(client, headers, "1.1.0")["version_id"],
        0,
        tenant_allowlist=["tenant-a"],
    )

    shadow = client.post(
        f"/v1/releases/{candidate['release_id']}/start-shadow", headers=headers,
        json={
            "shadow_sample_rate": 1.0,
            "side_effect_policy_version": "shadow-policy/test-v1",
            "side_effect_policy": {"irreversible_write": "simulate"},
            "shadow_resource_budget": {"max_qps": 5},
        },
    )
    assert shadow.status_code == 200, shadow.text
    assert shadow.json()["projection"]["release_stage"] == "shadow"
    assert shadow.json()["projection"]["revision"] == 2

    normal = client.get(
        "/v1/runtime/agents/customer-service/resolve", headers=headers,
        params={"environment": "production", "session_id": "ordinary-user-session"},
    )
    assert normal.status_code == 200, normal.text
    assert normal.json()["release_id"] == stable["release_id"]
    assert normal.json()["release_projection"]["release_stage"] == "production"

    mirrored = client.get(
        "/v1/runtime/agents/customer-service/resolve-shadow", headers=headers,
        params={"environment": "production", "session_id": "mirror-session"},
    )
    assert mirrored.status_code == 200, mirrored.text
    assert mirrored.json()["release_id"] == candidate["release_id"]
    assert mirrored.json()["assignment"] == "shadow"
    assert mirrored.json()["shadow_sampled"] is True

    class PassedShadowGate:
        async def gate_decision(self, tenant_id: str, decision_id: str) -> dict[str, object]:
            assert tenant_id == "tenant-a"
            assert decision_id == "gate-shadow"
            return {
                "releaseId": candidate["release_id"],
                "snapshotId": candidate["version_id"],
                "decision": "PROMOTE",
            }

    client.app.state.container.service._governance = PassedShadowGate()
    canary = client.post(
        f"/v1/releases/{candidate['release_id']}/start-canary", headers=headers,
        json={
            "rollout_percentage": 5,
            "decision_id": "gate-shadow",
            "eligible_roles": ["trusted-pilot"],
        },
    )
    assert canary.status_code == 200, canary.text
    assert canary.json()["projection"]["release_stage"] == "canary"
    assert canary.json()["projection"]["revision"] == 3
    # Tenant allow-list alone no longer bypasses the published IdP-role narrowing rule.
    excluded = client.get(
        "/v1/runtime/agents/customer-service/resolve",
        headers=headers,
        params={"environment": "production", "session_id": "role-excluded"},
    )
    assert excluded.json()["release_id"] == stable["release_id"]
    included = client.get(
        "/v1/runtime/agents/customer-service/resolve",
        headers={**headers, "X-Subject-Roles": "trusted-pilot"},
        params={"environment": "production", "session_id": "role-included"},
    )
    assert included.json()["release_id"] == candidate["release_id"]


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


def test_agent_release_requires_matching_agent_lab_evidence(
    client: TestClient,
    headers: dict[str, str],
    valid_spec: dict[str, object],
) -> None:
    """验证正式发布只能引用同 Agent、同版本、同 Judge Run 的 Agent Lab 实验。"""
    _create_agent(client, headers, valid_spec)
    version = _publish(client, headers, "3.1.0")

    async def passed_gate(tenant_id: str, run_id: str) -> dict:
        return {"id": "gate-lab", "passed": True, "metrics": {}, "reasons": []}

    async def evidence(tenant_id: str, experiment_id: str) -> dict:
        assert experiment_id == "alx-approved"
        return {
            "agentId": "customer-service",
            "versionId": version["version_id"],
            "environment": "laboratory",
            "judgeRunId": "judge-lab-1",
        }

    container = client.app.state.container
    container.governance_quality.quality_gate = passed_gate
    container.agent_lab.approved_release_evidence = evidence
    container.service._require_agent_lab = True
    response = client.post(
        "/v1/agents/customer-service/releases",
        headers=headers,
        json={
            "version_id": version["version_id"],
            "environment": "production",
            "quality_gate_run_id": "judge-lab-1",
            "agent_lab_experiment_id": "alx-approved",
        },
    )

    assert response.status_code == 201, response.text
    assert response.json()["agent_lab_experiment_id"] == "alx-approved"
