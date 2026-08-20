from typing import Protocol

import httpx
from opentelemetry import trace
from platform_infra.identity import WorkloadTokenProvider

from platform_sdk.contracts.rag import (
    RagCapabilitiesResponse,
    RagIndexVersionResponse,
    RagSearchRequest,
    RagSearchResponse,
)


class RagQueryClient(Protocol):
    """定义 Runtime 所需的最小只读 RAG 契约，不暴露索引或存储实现。"""

    def search(self, request: RagSearchRequest) -> RagSearchResponse:
        """按请求中的租户、权限和检索策略返回已过滤的证据，不暴露底层索引实现。"""
        ...

    def index_version(self) -> RagIndexVersionResponse:
        """返回当前活动不可变索引版本，供发布校验和故障诊断使用。"""
        ...


class LocalRagQueryClient:
    """本地开发适配器；生产 Runtime 应使用 HTTP/mTLS 边界。"""

    def __init__(self, service) -> None:
        """接收实现稳定 query 契约的本地服务对象。"""
        self.service = service

    def search(self, request: RagSearchRequest) -> RagSearchResponse:
        """执行租户范围内的检索，ACL 判断仍由本地服务负责。"""
        return self.service.search(request)

    def index_version(self) -> RagIndexVersionResponse:
        """暴露当前不可变索引版本，供发布/诊断进行兼容性检查。"""
        return RagIndexVersionResponse(
            index_version=self.service.index_version,
            backend=self.service.backend,
            embedding_contract=getattr(self.service, "embedding_contract", None),
        )


class HttpRagQueryClient:
    """带工作负载身份与可选 mTLS 的远程只读 RAG 客户端。"""

    def __init__(
        self,
        base_url: str,
        service_api_key: str = "",
        timeout: float = 30.0,
        workload_identity: WorkloadTokenProvider | None = None,
        *,
        mtls: dict | None = None,
    ) -> None:
        """初始化连接池；RAG 地址只允许由部署配置提供，不能由请求覆盖。"""
        self.base_url = base_url.rstrip("/")
        self.service_api_key = service_api_key
        self.timeout = timeout
        self.workload_identity = workload_identity
        self.client = httpx.Client(timeout=timeout, **(mtls or {}))

    def search(self, request: RagSearchRequest) -> RagSearchResponse:
        """调用查询 API；网络异常交给 Runtime 按证据必需策略失败或降级。"""
        with trace.get_tracer(__name__).start_as_current_span("context.rag_query"):
            response = self.client.post(
                f"{self.base_url}/api/v1/query/search",
                json=request.model_dump(mode="json"),
                headers=self._headers(),
                timeout=self.timeout,
            )
            response.raise_for_status()
            return RagSearchResponse.model_validate(response.json())

    def index_version(self) -> RagIndexVersionResponse:
        """查询活动索引版本，不读取或修改任何索引内容。"""
        response = self.client.get(
            f"{self.base_url}/api/v1/query/index-version",
            headers=self._headers(),
            timeout=self.timeout,
        )
        response.raise_for_status()
        return RagIndexVersionResponse.model_validate(response.json())

    def capabilities(self) -> RagCapabilitiesResponse:
        """读取服务声明的检索能力，供健康检查和发布兼容性校验。"""
        response = self.client.get(
            f"{self.base_url}/api/v1/query/capabilities",
            headers=self._headers(),
            timeout=self.timeout,
        )
        response.raise_for_status()
        return RagCapabilitiesResponse.model_validate(response.json())

    def _headers(self) -> dict[str, str]:
        """合并过渡服务密钥与工作负载令牌，不伪造最终用户权限。"""
        headers = (
            {"X-Rag-Agent-Key": self.service_api_key} if self.service_api_key else {}
        )
        if self.workload_identity is not None:
            headers.update(self.workload_identity.authorization_header())
        return headers

    def close(self) -> None:
        """关闭 HTTP 连接池。"""
        self.client.close()
