"""Authenticated Tool Gateway client used by the Runtime graph.

The client derives stable idempotency keys from an agent step and forwards the
execution context; it does not perform local authorization or side effects.
"""

from __future__ import annotations

import hashlib
import json
from threading import Lock
from uuid import uuid4

import httpx
from opentelemetry import trace
from platform_infra.identity import WorkloadTokenProvider

from platform_sdk.tools.registry import ToolContext, ToolRegistryError


class ToolGatewayClient:
    """Synchronous execution-plane client used by the bounded Agent graph."""

    def __init__(
        self,
        base_url: str,
        service_api_key: str,
        timeout: float = 30.0,
        *,
        client: httpx.Client | None = None,
        workload_identity: WorkloadTokenProvider | None = None,
    ) -> None:
        """初始化 Tool Gateway 传输边界和版本缓存。

        缓存只帮助在一次运行内复用目录版本，最终允许性仍由 Gateway 以快照、权限、
        审批和幂等键校验，客户端从不承担本地授权。
        """
        self.base_url = base_url.rstrip("/")
        self.service_api_key = service_api_key
        self.timeout = timeout
        self.client = client or httpx.Client(timeout=timeout)
        self._owns_client = client is None
        self.workload_identity = workload_identity
        self._versions: dict[tuple[str, str], str] = {}
        self._versions_lock = Lock()

    def manifests(
        self,
        permissions: frozenset[str],
        *,
        tenant_id: str = "default",
        user_id: str = "agent-runtime",
        request_id: str = "",
    ) -> list[dict]:
        """发现当前身份可见的工具清单并缓存其版本，供后续显式版本调用。"""
        with trace.get_tracer(__name__).start_as_current_span("runtime.tool_discovery"):
            response = self.client.get(
                f"{self.base_url}/api/v1/tools",
                headers=self._headers(
                    tenant_id,
                    user_id,
                    permissions,
                    request_id or f"tool-discovery-{uuid4().hex}",
                ),
                timeout=self.timeout,
            )
        self._raise_for_gateway_error(response)
        payload = response.json()
        if not isinstance(payload, list):
            raise ToolRegistryError("tool-gateway returned an invalid manifest list")
        with self._versions_lock:
            for manifest in payload:
                name = manifest.get("name")
                version = manifest.get("version")
                if isinstance(name, str) and isinstance(version, str):
                    self._versions[(tenant_id, name)] = version
        return payload

    def execute(self, name: str, arguments: dict, context: ToolContext):
        """以确定性幂等键调用一个目录工具版本。

        执行上下文携带租户、审批、发布快照及剩余尝试数；Gateway 返回待审批不会被
        误当作成功输出，而是交给 Runtime 状态机中断。
        """
        idempotency_key = context.idempotency_key or _idempotency_key(
            context.request_id, name, arguments
        )
        headers = self._headers(
            context.tenant_id,
            context.user_id,
            context.permissions,
            context.request_id,
        )
        headers.update(_execution_headers(context))
        headers["X-Idempotency-Key"] = idempotency_key
        with self._versions_lock:
            version = context.tool_version or self._versions.get(
                (context.tenant_id, name)
            )
        with trace.get_tracer(__name__).start_as_current_span(
            "runtime.tool_execute"
        ) as span:
            span.set_attribute("tool.name", name)
            span.set_attribute("tenant.id", context.tenant_id)
            response = self.client.post(
                f"{self.base_url}/api/v1/tools/{name}/invoke",
                headers=headers,
                json={
                    "arguments": arguments,
                    **({"version": version} if version else {}),
                    **(
                        {"approval_id": context.approval_id}
                        if context.approval_id
                        else {}
                    ),
                },
                timeout=self.timeout,
            )
        self._raise_for_gateway_error(response)
        payload = response.json()
        if payload.get("status") == "PENDING_APPROVAL":
            return {
                "status": "PENDING_APPROVAL",
                "approval_id": payload.get("approval_id"),
                "tool_name": name,
            }
        if payload.get("status") != "SUCCEEDED":
            raise ToolRegistryError(
                f"tool-gateway returned unexpected status: {payload.get('status')}"
            )
        return payload.get("output")

    def execution_status(self, name: str, context: ToolContext) -> dict:
        """查询已持久化工具幂等键的状态，供崩溃恢复先对账再决定是否重试。

        该接口只读取 Tool Gateway 的执行账本，不会触发工具或消耗审批。写工具缺少
        幂等键时不允许查询，避免 Runtime 用不稳定的参数猜测一次副作用。
        """
        if not context.idempotency_key:
            raise ToolRegistryError("tool execution status requires an idempotency key")
        headers = self._headers(
            context.tenant_id,
            context.user_id,
            context.permissions,
            context.request_id,
        )
        headers.update(_execution_headers(context))
        headers["X-Idempotency-Key"] = context.idempotency_key
        response = self.client.get(
            f"{self.base_url}/api/v1/tools/{name}/executions/current",
            headers=headers,
            timeout=self.timeout,
        )
        self._raise_for_gateway_error(response)
        payload = response.json()
        if not isinstance(payload, dict):
            raise ToolRegistryError("tool-gateway returned an invalid execution status")
        return payload

    def healthcheck(self) -> None:
        """探测 Gateway 就绪性；不携带业务请求或触发任何工具副作用。"""
        response = self.client.get(
            f"{self.base_url}/api/v1/health/ready",
            timeout=min(self.timeout, 5),
        )
        response.raise_for_status()

    def close(self) -> None:
        """仅关闭由本实例创建的客户端，避免误关闭注入的共享连接池。"""
        if self._owns_client:
            self.client.close()

    def _headers(
        self,
        tenant_id: str,
        user_id: str,
        permissions: frozenset[str],
        request_id: str,
    ) -> dict[str, str]:
        """生成最小身份与权限头；权限排序保证审计与缓存输入稳定。"""
        headers = {
            "X-Tenant-Id": tenant_id,
            "X-User-Id": user_id,
            "X-Permissions": ",".join(sorted(permissions)),
            "X-Request-Id": request_id,
        }
        if self.service_api_key:
            headers["X-Tool-Gateway-Key"] = self.service_api_key
        if self.workload_identity is not None:
            headers.update(self.workload_identity.authorization_header())
        return headers

    @staticmethod
    def _raise_for_gateway_error(response: httpx.Response) -> None:
        """将 Gateway 的结构化错误收敛为 SDK 异常，避免泄露非 JSON 响应内容。"""
        if response.status_code < 400:
            return
        try:
            error = response.json().get("error", {})
            code = error.get("code", "tool_gateway_error")
            message = error.get("message", f"HTTP {response.status_code}")
        except ValueError:
            code = "tool_gateway_error"
            message = f"HTTP {response.status_code}"
        raise ToolRegistryError(f"{code}: {message}")


def _idempotency_key(request_id: str, tool_name: str, arguments: dict) -> str:
    """对同一请求、工具和参数生成稳定哈希，安全支持网络重试而不重复副作用。"""
    canonical = json.dumps(
        {
            "request_id": request_id,
            "tool_name": tool_name,
            "arguments": arguments,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"agent-tool-{digest}"


def _execution_headers(context: ToolContext) -> dict[str, str]:
    """提取 Gateway 审计和预算执行头，剔除空值避免覆盖下游默认。"""
    values = {
        "X-Trace-Id": context.trace_id,
        "X-Run-Id": context.run_id,
        "X-Session-Id": context.session_id,
        "X-Tool-Execution-Id": context.tool_execution_id,
        "X-Root-Task-Id": context.root_task_id,
        "X-Business-Operation-Id": context.business_operation_id,
        "X-Operation-Id": context.operation_id,
        "X-Step-Id": context.step_id,
        "X-Plan-Id": context.plan_id,
        "X-Plan-Admission-Id": context.plan_admission_id,
        "X-Agent-Id": context.agent_id,
        "X-Agent-Version": context.agent_version,
        "X-Snapshot-Id": context.snapshot_id,
        "X-Deadline-At": context.deadline_at,
        "X-Attempt-Budget-Remaining": str(context.attempt_budget_remaining),
    }
    return {key: value for key, value in values.items() if value}
