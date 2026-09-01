"""Online governance windows must hold small samples and rollback hard safety failures."""

from fastapi.testclient import TestClient


def _trace(success: bool = True, **extra: object) -> dict[str, object]:
    """构造不含业务正文的线上 Trace，验证 Gate 只依赖可观察运行事实。"""
    return {
        "requestId": str(extra.pop("requestId", "req")), "traceId": "trace-1",
        "releaseId": "rel-1", "snapshotId": "snap-1", "success": success,
        "latencyMs": 100, "cost": 0.1, "request": {}, "response": {}, **extra,
    }


def test_online_gate_holds_small_windows_and_rolls_back_hard_safety_events(
    client: TestClient, auditor_headers: dict[str, str]
) -> None:
    """小样本不得提升；跨租户/PII/越权工具事件不等待均分而直接回滚。"""
    client.post("/v1/governance/evaluations/traces/gateway", headers=auditor_headers, json=_trace())
    hold = client.post("/v1/governance/evaluations/online/gate", headers=auditor_headers, json={
        "releaseId": "rel-1", "snapshotId": "snap-1", "policy": {"minSamples": 2},
    })
    assert hold.status_code == 200, hold.text
    assert hold.json()["decision"] == "HOLD"

    client.post(
        "/v1/governance/evaluations/traces/gateway", headers=auditor_headers,
        json=_trace(requestId="req-2", crossTenantAccess=True),
    )
    rollback = client.post("/v1/governance/evaluations/online/gate", headers=auditor_headers, json={
        "releaseId": "rel-1", "snapshotId": "snap-1", "policy": {"minSamples": 2},
    })
    assert rollback.status_code == 200, rollback.text
    assert rollback.json()["decision"] == "ROLLBACK"
