from __future__ import annotations

from fastapi.testclient import TestClient

from tests.conftest import event


def test_ingestion_is_idempotent_and_audits_unapproved_high_risk_tool(
    client: TestClient, auditor_headers: dict[str, str]
) -> None:
    body = event(
        "evt-tool-001",
        "tool.execution.completed",
        {
            "subject_type": "agent_run",
            "subject_id": "run-001",
            "tool_name": "payments.refund",
            "risk": "write_high_risk",
            "approval_granted": False,
        },
    )
    first = client.post("/v1/governance/events", json=body)
    assert first.status_code == 202, first.text
    assert first.json()["accepted"] is True
    assert len(first.json()["finding_ids"]) == 1

    duplicate = client.post("/v1/governance/events", json=body)
    assert duplicate.status_code == 202
    assert duplicate.json() == {"accepted": False, "duplicate": True, "finding_ids": []}

    audit = client.get("/v1/governance/audit-events", headers=auditor_headers)
    assert audit.status_code == 200
    assert [item["event_id"] for item in audit.json()["items"]] == ["evt-tool-001"]

    findings = client.get("/v1/governance/findings", headers=auditor_headers)
    assert findings.status_code == 200
    finding = findings.json()["items"][0]
    assert finding["rule_id"] == "tool.approval_required"
    assert finding["severity"] == "critical"


def test_policy_evaluates_model_region_evidence_cost_and_latency(
    client: TestClient, auditor_headers: dict[str, str]
) -> None:
    policy = client.put(
        "/v1/governance/tenant-policy",
        headers=auditor_headers,
        json={
            "allowed_models": ["approved-model"],
            "allowed_data_regions": ["cn"],
            "require_evidence_for_answer": True,
            "max_run_cost_usd": 1.0,
            "max_run_latency_ms": 1_000,
        },
    )
    assert policy.status_code == 200

    llm = client.post(
        "/v1/governance/events",
        json=event(
            "evt-llm-001",
            "llm.request.completed",
            {"model": "unapproved-model", "data_region": "us", "run_id": "run-002"},
        ),
    )
    assert llm.status_code == 202
    assert len(llm.json()["finding_ids"]) == 2

    run = client.post(
        "/v1/governance/events",
        json=event(
            "evt-run-001",
            "agent.run.completed",
            {"run_id": "run-002", "evidence_count": 0, "cost_usd": 1.2, "latency_ms": 1_100},
        ),
    )
    assert run.status_code == 202
    assert len(run.json()["finding_ids"]) == 3

    report = client.get("/v1/governance/reports/compliance", headers=auditor_headers)
    assert report.status_code == 200
    assert report.json()["total_events"] == 3
    assert report.json()["findings_by_severity"]["high"] == 2
    assert report.json()["findings_by_severity"]["medium"] == 3
    assert report.json()["compliance_status"] == "violation"


def test_findings_can_be_resolved_and_tenants_cannot_read_each_other(
    client: TestClient, auditor_headers: dict[str, str]
) -> None:
    response = client.post(
        "/v1/governance/events",
        json=event(
            "evt-tool-002",
            "tool.execution.completed",
            {"tool_name": "payments.refund", "risk": "write_high_risk", "approval_granted": False},
        ),
    )
    finding_id = response.json()["finding_ids"][0]
    resolved = client.post(
        f"/v1/governance/findings/{finding_id}/resolve",
        headers=auditor_headers,
        json={"note": "Approval was retrospectively attached to the incident."},
    )
    assert resolved.status_code == 200
    assert resolved.json()["status"] == "resolved"
    assert resolved.json()["resolved_by"] == "auditor@example.com"

    other_tenant = {**auditor_headers, "X-Tenant-Id": "tenant-b"}
    isolated = client.get("/v1/governance/findings", headers=other_tenant)
    assert isolated.status_code == 200
    assert isolated.json()["items"] == []
