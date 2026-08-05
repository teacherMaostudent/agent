from __future__ import annotations

from datetime import datetime
from typing import Any

import httpx
from platform_infra.identity import build_workload_token_provider
from platform_infra.mtls import mtls_httpx_options

from app.core.config import Settings


class GatewayPolicyClient:
    """Executes route reads/mutations; orchestration remains in Control Plane."""

    def __init__(self, settings: Settings) -> None:
        """Initialize GatewayPolicyClient dependencies and local state."""
        self._settings = settings

    def _auth(self) -> httpx.BasicAuth:
        """Internal helper for GatewayPolicyClient; preserve its caller-facing invariant."""
        return httpx.BasicAuth(
            self._settings.llm_gateway_admin_username,
            self._settings.llm_gateway_admin_password,
        )

    def _client_options(self) -> dict[str, Any]:
        """Internal helper for GatewayPolicyClient; preserve its caller-facing invariant."""
        return mtls_httpx_options(
            enabled=self._settings.mtls_enabled,
            ca_file=self._settings.mtls_ca_file,
            cert_file=self._settings.mtls_cert_file,
            key_file=self._settings.mtls_key_file,
        )

    async def route(self, route_name: str) -> dict[str, Any]:
        """Perform route within the GatewayPolicyClient ownership boundary."""
        async with httpx.AsyncClient(
            base_url=self._settings.llm_gateway_base_url,
            auth=self._auth(),
            timeout=30,
            **self._client_options(),
        ) as client:
            response = await client.get("/admin/routes")
            response.raise_for_status()
            routes = response.json()
        if route_name not in routes:
            raise ValueError(f"Unknown route: {route_name}")
        return routes[route_name]

    async def upsert_route(self, route_name: str, route: dict[str, Any]) -> dict[str, Any]:
        """Persist state while preserving the transaction and audit boundary."""
        async with httpx.AsyncClient(
            base_url=self._settings.llm_gateway_base_url,
            auth=self._auth(),
            timeout=30,
            **self._client_options(),
        ) as client:
            response = await client.put(f"/admin/routes/{route_name}", json=route)
            response.raise_for_status()
            return response.json()

    async def performance_summary(
        self, since: datetime, route_name: str, target: str
    ) -> dict[str, Any]:
        """Perform performance summary within the GatewayPolicyClient ownership boundary."""
        provider, model = target.split(":", 1)
        async with httpx.AsyncClient(
            base_url=self._settings.llm_gateway_base_url,
            auth=self._auth(),
            timeout=30,
            **self._client_options(),
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
        """Initialize GovernanceQualityClient dependencies and local state."""
        self._settings = settings
        self._workload_identity = build_workload_token_provider(settings)

    async def quality_gate(self, tenant_id: str, run_id: str) -> dict[str, Any]:
        """Perform quality gate within the GovernanceQualityClient ownership boundary."""
        headers = {
            "X-Tenant-Id": tenant_id,
            "X-User-Id": self._settings.governance_user_id,
            "X-Roles": "governance-auditor",
        }
        headers.update(self._workload_identity.authorization_header())
        if self._settings.governance_auditor_api_key:
            headers["X-Governance-Auditor-Key"] = self._settings.governance_auditor_api_key
        async with httpx.AsyncClient(
            base_url=self._settings.governance_base_url,
            timeout=30,
            **mtls_httpx_options(
                enabled=self._settings.mtls_enabled,
                ca_file=self._settings.mtls_ca_file,
                cert_file=self._settings.mtls_cert_file,
                key_file=self._settings.mtls_key_file,
            ),
        ) as client:
            response = await client.post(
                f"/v1/governance/evaluations/judge-runs/{run_id}/quality-gate",
                headers=headers,
            )
            response.raise_for_status()
            return response.json()
