from typing import Protocol

import httpx
from opentelemetry import trace

from app.contracts.context import (
    ContextAssembleRequest,
    ContextPackage,
    ConversationMessage,
)


class ContextClient(Protocol):
    def assemble(
        self,
        request: ContextAssembleRequest,
        *,
        execution_headers: dict[str, str] | None = None,
    ) -> ContextPackage: ...

    def append_message(
        self,
        session_id: str,
        message: ConversationMessage,
        tenant_id: str = "default",
        user_id: str = "anonymous",
    ) -> None: ...


class LocalContextClient:
    def __init__(self, service) -> None:
        self.service = service

    def assemble(
        self,
        request: ContextAssembleRequest,
        *,
        execution_headers: dict[str, str] | None = None,
    ) -> ContextPackage:
        del execution_headers
        return self.service.assemble(request)

    def append_message(
        self,
        session_id: str,
        message: ConversationMessage,
        tenant_id: str = "default",
        user_id: str = "anonymous",
    ) -> None:
        self.service.append_message(session_id, message, tenant_id, user_id)


class HttpContextClient:
    def __init__(
        self, base_url: str, service_api_key: str = "", timeout: float = 30.0
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.headers = {"X-Rag-Agent-Key": service_api_key} if service_api_key else {}

    def assemble(
        self,
        request: ContextAssembleRequest,
        *,
        execution_headers: dict[str, str] | None = None,
    ) -> ContextPackage:
        with trace.get_tracer(__name__).start_as_current_span(
            "runtime.context_assemble"
        ):
            response = httpx.post(
                f"{self.base_url}/api/v1/context/assemble",
                json=request.model_dump(mode="json"),
                headers={**self.headers, **(execution_headers or {})},
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
        response = httpx.post(
            f"{self.base_url}/api/v1/context/sessions/{session_id}/messages",
            json=message.model_dump(mode="json"),
            headers={
                **self.headers,
                "X-Tenant-Id": tenant_id,
                "X-User-Id": user_id,
            },
            timeout=self.timeout,
        )
        response.raise_for_status()
