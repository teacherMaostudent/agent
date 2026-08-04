from typing import Protocol

import httpx
from opentelemetry import trace
from platform_infra.identity import WorkloadTokenProvider

from app.contracts.rag import RagSearchRequest, RagSearchResponse


class RagQueryClient(Protocol):
    def search(self, request: RagSearchRequest) -> RagSearchResponse: ...


class LocalRagQueryClient:
    def __init__(self, service) -> None:
        self.service = service

    def search(self, request: RagSearchRequest) -> RagSearchResponse:
        return self.service.search(request)


class HttpRagQueryClient:
    def __init__(
        self,
        base_url: str,
        service_api_key: str = "",
        timeout: float = 30.0,
        workload_identity: WorkloadTokenProvider | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.service_api_key = service_api_key
        self.timeout = timeout
        self.workload_identity = workload_identity

    def search(self, request: RagSearchRequest) -> RagSearchResponse:
        headers = {"X-Rag-Agent-Key": self.service_api_key} if self.service_api_key else {}
        if self.workload_identity is not None:
            headers.update(self.workload_identity.authorization_header())
        with trace.get_tracer(__name__).start_as_current_span("context.rag_query"):
            response = httpx.post(
                f"{self.base_url}/api/v1/query/search",
                json=request.model_dump(mode="json"),
                headers=headers,
                timeout=self.timeout,
            )
            response.raise_for_status()
            return RagSearchResponse.model_validate(response.json())
