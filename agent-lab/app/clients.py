"""Agent Lab 与线上服务之间的 HTTP 契约客户端；不导入任何对方应用实现。"""

from __future__ import annotations

from typing import Any

import httpx
from platform_infra.identity import WorkloadTokenProvider


class _ServiceClient:
    """封装工作负载身份与 mTLS 选项，避免三个下游客户端各自实现不一致的安全边界。"""

    def __init__(
        self,
        base_url: str,
        timeout: float,
        workload_identity: WorkloadTokenProvider | None = None,
        mtls_options: dict[str, Any] | None = None,
    ) -> None:
        """保存不可变网络配置；每次请求仍使用短生命周期客户端避免连接泄漏。"""
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._workload_identity = workload_identity
        self._mtls_options = mtls_options or {}

    def _headers(self, tenant_id: str, user_id: str = "agent-lab") -> dict[str, str]:
        """生成受认证工作负载可委托的租户上下文，身份 Header 不信任终端调用方输入。"""
        headers = {"X-Tenant-Id": tenant_id, "X-User-Id": user_id}
        if self._workload_identity is not None:
            headers.update(self._workload_identity.authorization_header())
        return headers

    def _client(self) -> httpx.Client:
        """创建携带强制 mTLS 配置的同步客户端，证书缺失会在容器启动时提前失败。"""
        return httpx.Client(
            base_url=self._base_url,
            timeout=self._timeout,
            **self._mtls_options,
        )


class ControlPlaneClient(_ServiceClient):
    """只解析已发布快照，不读取草稿或执行发布操作。"""

    def __init__(
        self,
        base_url: str,
        runtime_key: str,
        timeout: float,
        workload_identity: WorkloadTokenProvider | None = None,
        mtls_options: dict[str, Any] | None = None,
    ) -> None:
        """保留迁移期 Runtime Key，同时优先使用可轮换的工作负载令牌。"""
        super().__init__(base_url, timeout, workload_identity, mtls_options)
        self._runtime_key = runtime_key

    def resolve(
        self,
        tenant_id: str,
        agent_id: str,
        environment: str,
        session_id: str,
    ) -> dict[str, Any]:
        """为一个回放 Session 固定当前已发布快照，禁止实验引用可变草稿。"""
        headers = self._headers(tenant_id)
        if self._runtime_key:
            headers["X-Runtime-Key"] = self._runtime_key
        with self._client() as client:
            response = client.get(
                f"/v1/runtime/agents/{agent_id}/resolve",
                params={"environment": environment, "session_id": session_id},
                headers=headers,
            )
            response.raise_for_status()
            return response.json()


class RuntimeClient(_ServiceClient):
    """调用 Runtime 的公开执行 API，不嵌入 Graph、Planner 或 Harness 实现。"""

    def run(
        self,
        payload: dict[str, Any],
        tenant_id: str,
        session_id: str,
        request_id: str,
    ) -> dict[str, Any]:
        """执行冻结快照；稳定 request ID 让 Worker 重试落到 Runtime 的幂等边界。"""
        headers = self._headers(tenant_id)
        headers.update(
            {
                "X-Permissions": "rag:read",
                "X-Request-Id": request_id,
                "X-Trace-Id": request_id,
                "X-Run-Id": request_id,
            }
        )
        with self._client() as client:
            response = client.post("/agent/run", json=payload, headers=headers)
            response.raise_for_status()
            return response.json()


class GovernanceClient(_ServiceClient):
    """把候选答案交给 Governance 评测，不在 Lab 内复制 Judge 或门禁规则。"""

    def __init__(
        self,
        base_url: str,
        auditor_key: str,
        timeout: float,
        workload_identity: WorkloadTokenProvider | None = None,
        mtls_options: dict[str, Any] | None = None,
    ) -> None:
        """保留迁移期 Auditor Key；生产请求同时绑定 Agent Lab 工作负载身份。"""
        super().__init__(base_url, timeout, workload_identity, mtls_options)
        self._auditor_key = auditor_key

    def judge(self, tenant_id: str, request: dict[str, Any]) -> dict[str, Any]:
        """创建冻结 Judge Run，候选答案来自本次回放而非可变线上请求。"""
        headers = self._headers(tenant_id)
        if self._auditor_key:
            headers["X-Auditor-Key"] = self._auditor_key
        with self._client() as client:
            response = client.post(
                "/v1/governance/evaluations/judge-runs", json=request, headers=headers
            )
            response.raise_for_status()
            return response.json()

    def quality_gate(self, tenant_id: str, run_id: str, request: dict[str, Any]) -> dict[str, Any]:
        """请求 Governance 用既定 Hard Gate 评定本次 Judge Run，Lab 不自行解释分数。"""
        headers = self._headers(tenant_id)
        if self._auditor_key:
            headers["X-Auditor-Key"] = self._auditor_key
        with self._client() as client:
            response = client.post(
                f"/v1/governance/evaluations/judge-runs/{run_id}/quality-gate",
                json=request,
                headers=headers,
            )
            response.raise_for_status()
            return response.json()
