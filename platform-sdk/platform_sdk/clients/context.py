"""Context Service transport boundary used by the Runtime graph."""

from typing import Protocol

import httpx
from opentelemetry import trace
from platform_infra.identity import WorkloadTokenProvider

from platform_sdk.contracts.artifacts import TaskArtifact, TaskArtifactTextCreate
from platform_sdk.contracts.context import (
    ContextAssembleRequest,
    ContextPackage,
    ConversationMessage,
)


class ContextClient(Protocol):
    """Assemble tenant-scoped prompt context from memory and optional evidence."""

    def assemble(
        self,
        request: ContextAssembleRequest,
        *,
        execution_headers: dict[str, str] | None = None,
    ) -> ContextPackage:
        """为已鉴权请求汇集会话记忆和可选知识证据，返回受 Token 预算约束的上下文包。"""
        ...

    def append_message(
        self,
        session_id: str,
        message: ConversationMessage,
        tenant_id: str = "default",
        user_id: str = "anonymous",
    ) -> None:
        """在指定租户与用户边界内追加会话消息，不隐式继承其他调用方的身份。"""
        ...

    def list_task_artifacts(
        self, root_task_id: str, *, tenant_id: str, limit: int = 100
    ) -> list[TaskArtifact]:
        """读取已授权 RootTask 的工件索引；不返回对象正文或临时下载凭据。"""
        ...

    def artifact_download_url(
        self, root_task_id: str, artifact_id: str, *, tenant_id: str
    ) -> str:
        """为已授权 Artifact 获取短期数据面 URL；调用方不得缓存或记录该 URL。"""
        ...

    def artifact_download_authorization(
        self, root_task_id: str, artifact_id: str, *, tenant_id: str
    ) -> dict[str, object]:
        """Return URL expiry and range support after the caller's resource authorization."""
        ...

    def create_text_artifact(
        self, root_task_id: str, content: str, *, tenant_id: str, user_id: str
    ) -> TaskArtifact:
        """写入受控文本交付物；失败由 Runtime 按可选交付策略记录，不能影响主结果。"""
        ...


class LocalContextClient:
    """In-process adapter retained for local deployment and deterministic tests."""

    def __init__(self, service) -> None:
        """包装同进程 Context 实现，仅用于本地组合与确定性测试。"""
        self.service = service

    def assemble(
        self,
        request: ContextAssembleRequest,
        *,
        execution_headers: dict[str, str] | None = None,
    ) -> ContextPackage:
        """直接组装上下文；进程内调用没有 HTTP 身份头，权限仍由服务实现校验。"""
        del execution_headers
        return self.service.assemble(request)

    def append_message(
        self,
        session_id: str,
        message: ConversationMessage,
        tenant_id: str = "default",
        user_id: str = "anonymous",
    ) -> None:
        """写入一条会话消息；调用方必须显式提供租户与用户以避免默认串租户。"""
        self.service.append_message(session_id, message, tenant_id, user_id)

    def list_task_artifacts(
        self, root_task_id: str, *, tenant_id: str, limit: int = 100
    ) -> list[TaskArtifact]:
        """本地适配器不拥有 Artifact Store，避免测试代码绕过 Context 的服务边界。"""
        del root_task_id, tenant_id, limit
        return []

    def artifact_download_url(
        self, root_task_id: str, artifact_id: str, *, tenant_id: str
    ) -> str:
        """本地测试适配器没有对象存储数据面，明确返回不可交付而非伪造 URL。"""
        del root_task_id, artifact_id, tenant_id
        raise RuntimeError("artifact delivery is not available in local context adapter")

    def artifact_download_authorization(
        self, root_task_id: str, artifact_id: str, *, tenant_id: str
    ) -> dict[str, object]:
        """Local tests have no data plane and therefore cannot manufacture signed authorization."""
        del root_task_id, artifact_id, tenant_id
        raise RuntimeError("artifact delivery is not available in local context adapter")

    def create_text_artifact(
        self, root_task_id: str, content: str, *, tenant_id: str, user_id: str
    ) -> TaskArtifact:
        """本地适配器没有对象存储，不模拟成功的交付物。"""
        del root_task_id, content, tenant_id, user_id
        raise RuntimeError("artifact delivery is not available in local context adapter")


class HttpContextClient:
    """Authenticated remote Context Service adapter for distributed Runtime workers."""

    def __init__(
        self,
        base_url: str,
        service_api_key: str = "",
        timeout: float = 30.0,
        workload_identity: WorkloadTokenProvider | None = None,
        *,
        mtls: dict | None = None,
    ) -> None:
        """创建远程 Context 客户端并配置工作负载令牌与可选 mTLS。

        静态服务密钥仅是过渡兼容凭据；生产身份由 ``workload_identity`` 与 TLS 客户端
        证书共同证明。
        """
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.headers = {"X-Rag-Agent-Key": service_api_key} if service_api_key else {}
        self.workload_identity = workload_identity
        self.client = httpx.Client(timeout=timeout, **(mtls or {}))

    def _headers(self) -> dict[str, str]:
        """构造服务认证头，不复制调用者的业务身份或权限声明。"""
        return {
            **self.headers,
            **(
                self.workload_identity.authorization_header()
                if self.workload_identity is not None
                else {}
            ),
        }

    def assemble(
        self,
        request: ContextAssembleRequest,
        *,
        execution_headers: dict[str, str] | None = None,
    ) -> ContextPackage:
        """调用上下文组装 API，并透传 Runtime 生成的受控执行头。"""
        with trace.get_tracer(__name__).start_as_current_span(
            "runtime.context_assemble"
        ):
            response = self.client.post(
                f"{self.base_url}/api/v1/context/assemble",
                json=request.model_dump(mode="json"),
                headers={**self._headers(), **(execution_headers or {})},
                timeout=self.timeout,
            )
            response.raise_for_status()
            return ContextPackage.model_validate(response.json())

    def append_message(
        self,
        session_id: str,
        message: ConversationMessage,
        tenant_id: str = "default",
        user_id: str = "anonymous",
    ) -> None:
        """追加已认证用户消息；HTTP 错误保留给上层决定重试或降级。"""
        response = self.client.post(
            f"{self.base_url}/api/v1/context/sessions/{session_id}/messages",
            json=message.model_dump(mode="json"),
            headers={
                **self._headers(),
                "X-Tenant-Id": tenant_id,
                "X-User-Id": user_id,
            },
            timeout=self.timeout,
        )
        response.raise_for_status()

    def list_task_artifacts(
        self, root_task_id: str, *, tenant_id: str, limit: int = 100
    ) -> list[TaskArtifact]:
        """取得 Context 中的工件索引；Runtime 后续会再做浏览器资源授权和脱敏投影。"""
        response = self.client.get(
            f"{self.base_url}/api/v1/context/tasks/{root_task_id}/artifacts",
            params={"limit": min(max(limit, 1), 200)},
            headers={**self._headers(), "X-Tenant-Id": tenant_id},
            timeout=self.timeout,
        )
        response.raise_for_status()
        return [TaskArtifact.model_validate(item) for item in response.json()]

    def artifact_download_url(
        self, root_task_id: str, artifact_id: str, *, tenant_id: str
    ) -> str:
        """读取一次性短期下载 URL；只供 Runtime 资源授权后的 BFF 重定向使用。"""
        response = self.client.get(
            f"{self.base_url}/api/v1/context/tasks/{root_task_id}/artifacts/{artifact_id}/download-url",
            headers={**self._headers(), "X-Tenant-Id": tenant_id},
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = response.json()
        url = str(payload.get("url", ""))
        if not url.startswith(("https://", "http://")):
            raise RuntimeError("context returned an invalid artifact download URL")
        return url

    def artifact_download_authorization(
        self, root_task_id: str, artifact_id: str, *, tenant_id: str
    ) -> dict[str, object]:
        """Return the full short-lived data-plane authorization without logging its URL."""
        response = self.client.get(
            f"{self.base_url}/api/v1/context/tasks/{root_task_id}/artifacts/{artifact_id}/download-url",
            headers={**self._headers(), "X-Tenant-Id": tenant_id},
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = response.json()
        url = str(payload.get("url", ""))
        if not url.startswith(("https://", "http://")):
            raise RuntimeError("context returned an invalid artifact download URL")
        return {
            "url": url,
            "expires_in_seconds": int(payload.get("expires_in_seconds", 300)),
            "supports_range": bool(payload.get("supports_range", True)),
        }

    def create_text_artifact(
        self, root_task_id: str, content: str, *, tenant_id: str, user_id: str
    ) -> TaskArtifact:
        """将最终报告交给 Context 的对象存储边界，Runtime 不直接写入共享桶。"""
        response = self.client.post(
            f"{self.base_url}/api/v1/context/tasks/{root_task_id}/artifacts/text",
            json=TaskArtifactTextCreate(content=content).model_dump(mode="json"),
            headers={**self._headers(), "X-Tenant-Id": tenant_id, "X-User-Id": user_id},
            timeout=self.timeout,
        )
        response.raise_for_status()
        return TaskArtifact.model_validate(response.json())

    def close(self) -> None:
        """释放连接池和 TLS 资源，容器关闭时必须调用。"""
        self.client.close()
