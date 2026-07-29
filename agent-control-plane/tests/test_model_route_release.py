from __future__ import annotations

from copy import deepcopy
from typing import Any

from fastapi.testclient import TestClient


class FakeGateway:
    def __init__(self) -> None:
        self.current = {
            "primary": "provider:stable",
            "weighted": [],
            "canary": [],
            "fallbacks": ["provider:fallback"],
        }
        self.summary = {
            "requests": 25,
            "errorRate": 0,
            "timeoutRate": 0,
            "avgLatencyMs": 100,
            "costPerRequest": 0.01,
        }

    async def route(self, route_name: str) -> dict[str, Any]:
        assert route_name == "general"
        return deepcopy(self.current)

    async def upsert_route(
        self, route_name: str, route: dict[str, Any]
    ) -> dict[str, Any]:
        assert route_name == "general"
        self.current = deepcopy(route)
        return deepcopy(route)

    async def performance_summary(self, *_: Any) -> dict[str, Any]:
        return deepcopy(self.summary)


class FakeGovernance:
    async def quality_gate(self, tenant_id: str, run_id: str) -> dict[str, Any]:
        assert tenant_id == "tenant-a"
        return {
            "id": f"gate-{run_id}",
            "passed": True,
            "metrics": {"averageScore": 90},
            "reasons": [],
        }


def test_control_plane_owns_model_canary_promotion(
    client: TestClient, headers: dict[str, str]
) -> None:
    container = client.app.state.container
    gateway = FakeGateway()
    container.model_releases._gateway = gateway
    container.model_releases._governance = FakeGovernance()

    started = client.post(
        "/v1/model-route-releases",
        headers=headers,
        json={
            "routeName": "general",
            "canaryTarget": "provider:new",
            "judgeRunId": "judge-1",
            "canaryPercent": 5,
        },
    )
    assert started.status_code == 200, started.text
    assert started.json()["status"] == "CANARY_ACTIVE"
    assert gateway.current["canary"] == [{"target": "provider:new", "percent": 5}]

    monitored = client.post(
        f"/v1/model-route-releases/{started.json()['id']}/monitor",
        headers=headers,
    )
    assert monitored.status_code == 200
    assert monitored.json()["status"] == "PROMOTED"
    assert gateway.current["primary"] == "provider:new"
    assert gateway.current["canary"] == []
    assert gateway.current["fallbacks"][0] == "provider:stable"
