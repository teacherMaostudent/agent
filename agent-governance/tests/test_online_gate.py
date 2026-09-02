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


def test_online_gate_rejects_false_side_effect_before_promotion(
    client: TestClient, auditor_headers: dict[str, str]
) -> None:
    """Shadow Gate 不能只看成功率；不该执行却准备执行的副作用必须阻断提升。"""
    response = client.post(
        "/v1/governance/evaluations/traces/gateway", headers=auditor_headers,
        json=_trace(
            requestId="shadow-side-effect", releaseStage="shadow",
            proposedSideEffect=True, expectedSideEffect=False,
            decisionCorrect=True, authorizationAgreement=True,
        ),
    )
    assert response.status_code == 200, response.text
    gate = client.post(
        "/v1/governance/evaluations/online/gate", headers=auditor_headers,
        json={
            "releaseId": "rel-1", "snapshotId": "snap-1",
            "policy": {"minSamples": 1, "maxFalseSideEffectRate": 0.0},
        },
    )
    assert gate.status_code == 200, gate.text
    assert gate.json()["decision"] == "ROLLBACK"
    assert gate.json()["metrics"]["falseSideEffectRate"] == 1.0
