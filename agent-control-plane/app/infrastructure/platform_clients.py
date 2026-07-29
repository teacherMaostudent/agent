from __future__ import annotations

from datetime import datetime
from typing import Any

import httpx

from app.core.config import Settings


class GatewayPolicyClient:
    """Executes route reads/mutations; orchestration remains in Control Plane."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def _auth(self) -> httpx.BasicAuth:
        return httpx.BasicAuth(
            self._settings.llm_gateway_admin_username,
            self._settings.llm_gateway_admin_password,
        )

    async def route(self, route_name: str) -> dict[str, Any]:
        async with httpx.AsyncClient(
            base_url=self._settings.llm_gateway_base_url, auth=self._auth(), timeout=30
        ) as client:
            response = await client.get("/admin/routes")
            response.raise_for_status()
            routes = response.json()
        if route_name not in routes:
            raise ValueError(f"Unknown route: {route_name}")
        return routes[route_name]

    async def upsert_route(self, route_name: str, route: dict[str, Any]) -> dict[str, Any]:
        async with httpx.AsyncClient(
            base_url=self._settings.llm_gateway_base_url, auth=self._auth(), timeout=30
        ) as client:
            response = await client.put(f"/admin/routes/{route_name}", json=route)
            response.raise_for_status()
            return response.json()

    async def performance_summary(
        self, since: datetime, route_name: str, target: str
    ) -> dict[str, Any]:
        provider, model = target.split(":", 1)
        async with httpx.AsyncClient(
            base_url=self._settings.llm_gateway_base_url, auth=self._auth(), timeout=30
        ) as client:
            response = await client.get(
                "/admin/reports/performance/summary",
                params={
                    "since": since.isoformat(),
                    "requestedModel": route_name,
                    "provider": provider,
                    "model": model,
                },
            )
            response.raise_for_status()
            return response.json()


class GovernanceQualityClient:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def quality_gate(self, tenant_id: str, run_id: str) -> dict[str, Any]:
        headers = {
            "X-Tenant-Id": tenant_id,
            "X-User-Id": self._settings.governance_user_id,
            "X-Roles": "governance-auditor",
        }
        async with httpx.AsyncClient(
            base_url=self._settings.governance_base_url, timeout=30
        ) as client:
            response = await client.post(
                f"/v1/governance/evaluations/judge-runs/{run_id}/quality-gate",
                headers=headers,
            )
            response.raise_for_status()
            return response.json()
