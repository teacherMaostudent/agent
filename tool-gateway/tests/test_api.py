from __future__ import annotations

from app.domain.models import SideEffectClass, ToolRisk

from .conftest import tool_spec


def test_auth_discovery_permission_and_tenant_filter(
    gateway_factory,
    trusted_headers,
) -> None:
    client = gateway_factory(
        [
            (tool_spec("visible"), lambda args, context: args),
            (
                tool_spec("other_permission", permission="secret:read"),
                lambda args, context: args,
            ),
            (
                tool_spec("other_tenant", enabled_tenants=["tenant-b"]),
                lambda args, context: args,
            ),
        ]
    )

    assert client.get("/api/v1/tools").status_code == 401
    response = client.get("/api/v1/tools", headers=trusted_headers)

    assert response.status_code == 200
    assert [item["name"] for item in response.json()] == ["visible"]
    assert response.json()[0]["parameters"]["additionalProperties"] is False


def test_discovery_selects_latest_version_enabled_for_tenant(
    gateway_factory,
    trusted_headers,
) -> None:
    client = gateway_factory(
        [
            (
                tool_spec("versioned", version="1.0.0", enabled_tenants=["tenant-a"]),
                lambda args, context: {"version": "1.0.0"},
            ),
            (
                tool_spec("versioned", version="2.0.0", enabled_tenants=["tenant-b"]),
                lambda args, context: {"version": "2.0.0"},
            ),
        ]
    )

    tenant_a = client.get("/api/v1/tools", headers=trusted_headers)
    tenant_b = client.get(
        "/api/v1/tools",
        headers={**trusted_headers, "X-Tenant-Id": "tenant-b"},
    )

    assert tenant_a.json()[0]["version"] == "1.0.0"
    assert tenant_b.json()[0]["version"] == "2.0.0"


def test_schema_permission_and_output_validation(gateway_factory, trusted_headers) -> None:
    client = gateway_factory(
        [
            (
                tool_spec(
                    "validated",
                    output_schema={
                        "type": "object",
                        "properties": {"ok": {"type": "boolean"}},
                        "required": ["ok"],
                        "additionalProperties": False,
                    },
                ),
                lambda args, context: {"ok": args["value"] == "ok"},
            ),
            (
                tool_spec(
                    "bad_output",
                    output_schema={
                        "type": "object",
                        "required": ["required_field"],
                    },
                ),
                lambda args, context: {"wrong": True},
            ),
        ]
    )

    invalid = client.post(
        "/api/v1/tools/validated/invoke",
        headers=trusted_headers,
        json={"arguments": {"value": "x"}},
    )
    denied_headers = {**trusted_headers, "X-Permissions": ""}
    denied = client.post(
        "/api/v1/tools/validated/invoke",
        headers=denied_headers,
        json={"arguments": {"value": "ok"}},
    )
    bad_output = client.post(
        "/api/v1/tools/bad_output/invoke",
        headers=trusted_headers,
        json={"arguments": {"value": "ok"}},
    )

    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "tool_arguments_invalid"
    assert denied.status_code == 403
    assert bad_output.status_code == 502
    assert bad_output.json()["error"]["code"] == "tool_output_invalid"


def test_idempotency_replay_conflict_and_audit_redaction(
    gateway_factory,
    trusted_headers,
) -> None:
    calls = {"count": 0}

    def handler(args, context):
        calls["count"] += 1
        return {"call": calls["count"], "received": args["value"]}

    spec = tool_spec(
        "write_record",
        permission="tool:write",
        risk=ToolRisk.WRITE_LOW_RISK,
        idempotent=False,
    )
    client = gateway_factory([(spec, handler)])
    headers = {**trusted_headers, "X-Idempotency-Key": "stable-key-0001"}

    first = client.post(
        "/api/v1/tools/write_record/invoke",
        headers=headers,
        json={"arguments": {"value": "sensitive-value"}},
    )
    replay = client.post(
        "/api/v1/tools/write_record/invoke",
        headers=headers,
        json={"arguments": {"value": "sensitive-value"}},
    )
    conflict = client.post(
        "/api/v1/tools/write_record/invoke",
        headers=headers,
        json={"arguments": {"value": "different-value"}},
    )
    audit = client.get("/api/v1/audit", headers=trusted_headers)

    assert first.status_code == 200
    assert replay.status_code == 200
    assert replay.json()["idempotent_replay"] is True
    assert replay.json()["output"] == first.json()["output"]
    assert calls["count"] == 1
    assert conflict.status_code == 409
    assert audit.status_code == 200
    serialized_audit = audit.text
    assert "sensitive-value" not in serialized_audit
    assert "stable-key-0001" not in serialized_audit
    assert audit.json()["count"] == 3


def test_shadow_write_returns_receipt_without_calling_external_adapter(
    gateway_factory, trusted_headers
) -> None:
    """Shadow 写操作必须保留授权事实，但不能因为模拟通过就触达外部 Adapter。"""
    calls = {"count": 0}

    def handler(args, context):
        calls["count"] += 1
        return {"committed": True}

    spec = tool_spec("shadow-write", permission="tool:write", risk=ToolRisk.WRITE_LOW_RISK)
    assert spec.side_effect_class is SideEffectClass.IRREVERSIBLE_WRITE
    client = gateway_factory([(spec, handler)])
    response = client.post(
        "/api/v1/tools/shadow-write/invoke",
        headers={
            **trusted_headers,
            "X-Permissions": "tool:write",
            "X-Idempotency-Key": "shadow-write-1",
            "X-Release-Id": "rel-shadow",
            "X-Release-Stage": "shadow",
        },
        json={"arguments": {"value": "ok"}},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert calls["count"] == 0
    assert payload["execution_receipt"] == {
        "release_id": "rel-shadow",
        "release_stage": "shadow",
        "release_projection_revision": 1,
        "side_effect_class": "irreversible_write",
        "commit_capability": "none",
        "outcome": "SIMULATED",
        "commit_performed": False,
    }
    assert payload["output"]["simulation_outcome"] == "SUCCESS"


def test_execution_status_reads_idempotency_ledger_without_reinvoking_tool(
    gateway_factory, trusted_headers
) -> None:
    """Runtime 恢复查询只读取幂等账本, 不能让查询本身造成第二次业务副作用。"""
    calls = {"count": 0}

    def handler(args, context):
        calls["count"] += 1
        return {"ok": args["value"]}

    client = gateway_factory(
        [(tool_spec("recoverable", permission="tool:write", risk=ToolRisk.WRITE_LOW_RISK), handler)]
    )
    headers = {**trusted_headers, "X-Idempotency-Key": "tool-execution-0001"}
    response = client.post(
        "/api/v1/tools/recoverable/invoke", headers=headers, json={"arguments": {"value": "ok"}}
    )
    status = client.get("/api/v1/tools/recoverable/executions/current", headers=headers)

    assert response.status_code == 200
    assert status.status_code == 200
    assert status.json()["status"] == "COMPLETED"
    assert status.json()["response"]["output"] == {"ok": "ok"}
    assert calls["count"] == 1


def test_write_requires_idempotency_key(gateway_factory, trusted_headers) -> None:
    spec = tool_spec(
        "write_required",
        permission="tool:write",
        risk=ToolRisk.WRITE_LOW_RISK,
        idempotent=False,
    )
    client = gateway_factory([(spec, lambda args, context: {"ok": True})])

    response = client.post(
        "/api/v1/tools/write_required/invoke",
        headers=trusted_headers,
        json={"arguments": {"value": "ok"}},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "idempotency_key_required"


def test_runtime_write_requires_admitted_step_identity(gateway_factory, trusted_headers) -> None:
    """带发布快照的写动作必须绑定准入计划和具体 operation; 避免把计划准入当作动作授权。"""
    client = gateway_factory(
        [
            (
                tool_spec(
                    "admitted_write",
                    permission="tool:write",
                    risk=ToolRisk.WRITE_LOW_RISK,
                ),
                lambda args, context: {"ok": True},
            )
        ]
    )
    base = {
        **trusted_headers,
        "X-Snapshot-Id": "snapshot-1",
        "X-Idempotency-Key": "admitted-write-0001",
    }
    rejected = client.post(
        "/api/v1/tools/admitted_write/invoke",
        headers=base,
        json={"arguments": {"value": "ok"}},
    )
    admitted = client.post(
        "/api/v1/tools/admitted_write/invoke",
        headers={
            **base,
            "X-Operation-Id": "operation-1",
            "X-Step-Id": "step-1",
            "X-Plan-Id": "plan-1",
            "X-Plan-Admission-Id": "admission-1",
        },
        json={"arguments": {"value": "ok"}},
    )

    assert rejected.status_code == 403
    assert admitted.status_code == 200
    assert admitted.json()["authorization"]["decision"] == "ALLOW"
    events = client.app.state.container.repository.pending_events()
    assert {item["event_type"] for item in events} == {
        "tool.authorization.decided",
        "tool.execution.completed",
    }


def test_execution_context_is_preserved_and_emits_durable_event(
    gateway_factory, trusted_headers
) -> None:
    received = {}

    def handler(args, context):
        received["context"] = context
        return {"ok": True}

    client = gateway_factory([(tool_spec("contextual"), handler)])
    response = client.post(
        "/api/v1/tools/contextual/invoke",
        headers={
            **trusted_headers,
            "X-Trace-Id": "trace-1",
            "X-Run-Id": "run-1",
            "X-Session-Id": "session-1",
            "X-Agent-Id": "review-agent",
            "X-Agent-Version": "1.2.3",
            "X-Snapshot-Id": "snapshot-1",
            "X-Deadline-At": "2030-01-01T00:00:00Z",
            "X-Attempt-Budget-Remaining": "3",
        },
        json={"arguments": {"value": "ok"}},
    )

    assert response.status_code == 200
    assert received["context"].run_id == "run-1"
    assert received["context"].snapshot_id == "snapshot-1"
    events = client.app.state.container.repository.pending_events()
    assert len(events) == 1
    assert events[0]["event_type"] == "tool.execution.completed"
    assert events[0]["payload"]["risk"] == "read_only"
    assert events[0]["payload"]["approval_granted"] is False
    assert events[0]["payload"]["run_id"] == "run-1"


def test_high_risk_approval_is_bound_and_consumed(
    gateway_factory,
    trusted_headers,
) -> None:
    calls = {"count": 0}

    def handler(args, context):
        calls["count"] += 1
        return {"submitted": args["value"]}

    spec = tool_spec(
        "dangerous_write",
        permission="tool:write",
        risk=ToolRisk.WRITE_HIGH_RISK,
        approval_required=True,
        idempotent=True,
    )
    client = gateway_factory([(spec, handler)])
    headers = {**trusted_headers, "X-Idempotency-Key": "dangerous-operation-001"}

    pending = client.post(
        "/api/v1/tools/dangerous_write/invoke",
        headers=headers,
        json={"arguments": {"value": "approved-value"}},
    )
    approval_id = pending.json()["approval_id"]
    approved = client.post(
        f"/api/v1/approvals/{approval_id}/approve",
        headers={**trusted_headers, "X-Tool-Gateway-Admin-Key": "admin-secret"},
        json={"reason": "ticket approved"},
    )
    executed = client.post(
        "/api/v1/tools/dangerous_write/invoke",
        headers=headers,
        json={
            "arguments": {"value": "approved-value"},
            "approval_id": approval_id,
        },
    )
    replay = client.post(
        "/api/v1/tools/dangerous_write/invoke",
        headers=headers,
        json={
            "arguments": {"value": "approved-value"},
            "approval_id": approval_id,
        },
    )
    approval = client.get(f"/api/v1/approvals/{approval_id}", headers=trusted_headers)

    assert pending.status_code == 202
    assert pending.json()["status"] == "PENDING_APPROVAL"
    assert approved.json()["status"] == "APPROVED"
    assert executed.status_code == 200
    assert executed.json()["output"] == {"submitted": "approved-value"}
    assert replay.json()["idempotent_replay"] is True
    assert calls["count"] == 1
    assert approval.json()["status"] == "CONSUMED"


def test_canary_never_commits_a_write_without_a_concrete_prepare_commit_adapter(
    gateway_factory,
    trusted_headers,
) -> None:
    """灰度百分比不是把普通 HTTP Adapter 变成可事务回滚 Adapter 的理由。"""
    calls = {"count": 0}
    spec = tool_spec(
        "reversible-write",
        permission="tool:write",
        risk=ToolRisk.WRITE_LOW_RISK,
        idempotent=True,
    )
    spec.side_effect_class = SideEffectClass.REVERSIBLE_WRITE
    client = gateway_factory([(spec, lambda args, context: calls.update(count=calls["count"] + 1))])

    response = client.post(
        "/api/v1/tools/reversible-write/invoke",
        headers={
            **trusted_headers,
            "X-Idempotency-Key": "canary-write-1",
            "X-Release-Stage": "canary",
        },
        json={"arguments": {"value": "ok"}},
    )

    assert response.status_code == 200
    assert calls["count"] == 0
    assert response.json()["execution_receipt"]["outcome"] == "SIMULATED"
    assert response.json()["execution_receipt"]["commit_performed"] is False


def test_approval_cannot_be_reused_with_changed_arguments(
    gateway_factory,
    trusted_headers,
) -> None:
    spec = tool_spec(
        "bound_approval",
        permission="tool:write",
        risk=ToolRisk.HUMAN_APPROVAL_REQUIRED,
        approval_required=True,
        idempotent=True,
    )
    client = gateway_factory([(spec, lambda args, context: {"ok": True})])
    headers = {**trusted_headers, "X-Idempotency-Key": "approval-bound-key"}
    pending = client.post(
        "/api/v1/tools/bound_approval/invoke",
        headers=headers,
        json={"arguments": {"value": "original"}},
    )
    approval_id = pending.json()["approval_id"]
    client.post(
        f"/api/v1/approvals/{approval_id}/approve",
        headers={**trusted_headers, "X-Tool-Gateway-Admin-Key": "admin-secret"},
        json={"reason": ""},
    )

    changed = client.post(
        "/api/v1/tools/bound_approval/invoke",
        headers={**headers, "X-Idempotency-Key": "approval-bound-other-key"},
        json={
            "arguments": {"value": "changed"},
            "approval_id": approval_id,
        },
    )

    assert changed.status_code == 403
    assert changed.json()["error"]["code"] == "approval_invalid"
