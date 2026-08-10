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
        """Invoke one catalogued version with a deterministic replay key."""
        idempotency_key = _idempotency_key(context.request_id, name, arguments)
        headers = self._headers(
            context.tenant_id,
            context.user_id,
            context.permissions,
            context.request_id,
        )
        headers.update(_execution_headers(context))
        headers["X-Idempotency-Key"] = idempotency_key
        with self._versions_lock:
            version = context.tool_version or self._versions.get((context.tenant_id, name))
        with trace.get_tracer(__name__).start_as_current_span("runtime.tool_execute") as span:
            span.set_attribute("tool.name", name)
            span.set_attribute("tenant.id", context.tenant_id)
            response = self.client.post(
                f"{self.base_url}/api/v1/tools/{name}/invoke",
                headers=headers,
                json={
                    "arguments": arguments,
                    **({"version": version} if version else {}),
                    **({"approval_id": context.approval_id} if context.approval_id else {}),
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

    def healthcheck(self) -> None:
        response = self.client.get(
            f"{self.base_url}/api/v1/health/ready",
            timeout=min(self.timeout, 5),
        )
        response.raise_for_status()

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def _headers(
        self,
        tenant_id: str,
        user_id: str,
        permissions: frozenset[str],
        request_id: str,
    ) -> dict[str, str]:
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
    values = {
        "X-Trace-Id": context.trace_id,
        "X-Run-Id": context.run_id,
        "X-Session-Id": context.session_id,
        "X-Agent-Id": context.agent_id,
        "X-Agent-Version": context.agent_version,
        "X-Snapshot-Id": context.snapshot_id,
        "X-Deadline-At": context.deadline_at,
        "X-Attempt-Budget-Remaining": str(context.attempt_budget_remaining),
    }
    return {key: value for key, value in values.items() if value}
