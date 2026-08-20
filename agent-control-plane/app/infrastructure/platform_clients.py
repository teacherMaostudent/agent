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
        """保存 Gateway 连接与凭据配置；实际连接仅在请求时短暂创建。"""
        self._settings = settings

    def _auth(self) -> httpx.BasicAuth:
        """构造 Gateway 管理 API 的基本认证；凭据仅来自受保护配置字段。"""
        return httpx.BasicAuth(
            self._settings.llm_gateway_admin_username,
            self._settings.llm_gateway_admin_password,
        )

    def _client_options(self) -> dict[str, Any]:
        """按服务配置传递 mTLS 信任链与客户端证书，禁止调用方自行绕过。"""
        return mtls_httpx_options(
            enabled=self._settings.mtls_enabled,
            ca_file=self._settings.mtls_ca_file,
            cert_file=self._settings.mtls_cert_file,
            key_file=self._settings.mtls_key_file,
        )

    async def route(self, route_name: str) -> dict[str, Any]:
        """读取已存在的 Gateway 路由；未知路由显式失败以阻止发布漂移。"""
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
        """将经发布编排确认的路由写入 Gateway；调用方负责 Saga 补偿与审计。"""
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
        """读取灰度窗口性能指标，供 Control Plane 判断自动提升或回滚。"""
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
    """调用 Governance 质量门禁；Control Plane 不复制评测与准入规则。"""

    def __init__(self, settings: Settings) -> None:
        """初始化工作负载令牌提供者，使跨服务请求绑定 Control Plane 身份。"""
        self._settings = settings
        self._workload_identity = build_workload_token_provider(settings)

    async def quality_gate(self, tenant_id: str, run_id: str) -> dict[str, Any]:
        """请求治理质量门禁；网络或认证失败必须阻断发布而不是默认放行。"""
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


class ModelLabClient:
    """Verify that an offline model artifact passed evaluation before online rollout."""

    def __init__(self, settings: Settings) -> None:
        """保存受认证 Model Lab 端点；工件校验发生在路由发布前。"""
        self._settings = settings
        self._workload_identity = build_workload_token_provider(settings)

    async def approved_artifact(self, tenant_id: str, experiment_id: str) -> dict[str, Any]:
        """仅返回已批准且含模型卡的实验工件，缺失任一条件即拒绝上线。"""
        async with httpx.AsyncClient(
            base_url=self._settings.model_lab_base_url,
            timeout=30,
            **mtls_httpx_options(
                enabled=self._settings.mtls_enabled,
                ca_file=self._settings.mtls_ca_file,
                cert_file=self._settings.mtls_cert_file,
                key_file=self._settings.mtls_key_file,
            ),
        ) as client:
            response = await client.get(
                f"/internal/v1/experiments/{experiment_id}",
                params={"tenant_id": tenant_id},
                headers={
                    "X-Model-Lab-Key": self._settings.model_lab_service_api_key or "",
                    "X-Tenant-Id": tenant_id,
                    **self._workload_identity.authorization_header(),
                },
            )
            response.raise_for_status()
            record = response.json()
        if record.get("status") != "APPROVED" or not record.get("model_card"):
            raise ValueError("Model Lab experiment is not approved with a model card")
        if record.get("plan", {}).get("tenant_id") != tenant_id:
            raise ValueError("Model Lab experiment tenant does not match release")
        return record


class AgentLabClient:
    """读取已完成 Agent 回放的发布证据；不创建实验或修改其结果。"""

    def __init__(self, settings: Settings) -> None:
        """保存 Agent Lab 地址和服务凭据，并为生产调用创建可轮换工作负载身份。"""
        self._settings = settings
        self._workload_identity = build_workload_token_provider(settings)

    async def approved_release_evidence(self, tenant_id: str, experiment_id: str) -> dict[str, Any]:
        """只接受已通过 Governance 门禁且绑定不可变快照的实验事实。"""
        async with httpx.AsyncClient(
            base_url=self._settings.agent_lab_base_url,
            timeout=30,
            **mtls_httpx_options(
                enabled=self._settings.mtls_enabled,
                ca_file=self._settings.mtls_ca_file,
                cert_file=self._settings.mtls_cert_file,
                key_file=self._settings.mtls_key_file,
            ),
        ) as client:
            response = await client.get(
                f"/internal/v1/experiments/{experiment_id}/release-evidence",
                params={"tenant_id": tenant_id},
                headers={
                    "X-Agent-Lab-Key": self._settings.agent_lab_service_api_key or "",
                    "X-Tenant-Id": tenant_id,
                    "X-User-Id": "agent-control-plane",
                    **self._workload_identity.authorization_header(),
                },
            )
            response.raise_for_status()
            return response.json()
