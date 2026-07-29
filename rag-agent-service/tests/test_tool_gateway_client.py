import json

import httpx

from app.tools.client import ToolGatewayClient
from app.tools.registry import ToolContext, ToolRegistryError


def test_client_discovers_and_executes_with_trusted_context() -> None:
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured[request.url.path] = {
            "tenant": request.headers.get("X-Tenant-Id"),
            "user": request.headers.get("X-User-Id"),
            "permissions": request.headers.get("X-Permissions"),
            "idempotency": request.headers.get("X-Idempotency-Key"),
        }
        if request.method == "GET":
            return httpx.Response(
                200,
                json=[
                    {
                        "name": "get_document",
                        "version": "1.0.0",
                        "description": "Read a document",
                        "parameters": {"type": "object"},
                    }
                ],
            )
        body = json.loads(request.content)
        captured["invoke_body"] = body
        return httpx.Response(
            200,
            json={
                "status": "SUCCEEDED",
                "tool_name": "get_document",
                "tool_version": "1.0.0",
                "output": {"document_id": body["arguments"]["document_id"]},
            },
        )

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = ToolGatewayClient(
        "http://tool-gateway",
        "trusted-secret",
        client=http_client,
    )
    context = ToolContext(
        tenant_id="tenant-a",
        user_id="user-a",
        permissions=frozenset({"document:read"}),
        request_id="request-a",
        approval_id="approval-123",
    )

    manifests = client.manifests(
        context.permissions,
        tenant_id=context.tenant_id,
        user_id=context.user_id,
        request_id=context.request_id,
    )
    result = client.execute("get_document", {"document_id": "doc-1"}, context)

    assert manifests[0]["name"] == "get_document"
    assert result == {"document_id": "doc-1"}
    invoke_headers = captured["/api/v1/tools/get_document/invoke"]
    assert invoke_headers["tenant"] == "tenant-a"
    assert invoke_headers["user"] == "user-a"
    assert invoke_headers["permissions"] == "document:read"
    assert invoke_headers["idempotency"].startswith("agent-tool-")
    assert captured["invoke_body"]["version"] == "1.0.0"
    assert captured["invoke_body"]["approval_id"] == "approval-123"


def test_client_surfaces_gateway_error_without_leaking_response_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json={
                "error": {
                    "code": "tool_permission_denied",
                    "message": "permission denied",
                    "details": {"secret": "not propagated"},
                }
            },
        )

    client = ToolGatewayClient(
        "http://tool-gateway",
        "trusted-secret",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    context = ToolContext("tenant-a", "user-a", frozenset(), "request-a")

    try:
        client.execute("secret_tool", {}, context)
    except ToolRegistryError as exc:
        message = str(exc)
    else:
        raise AssertionError("ToolRegistryError was not raised")

    assert message == "tool_permission_denied: permission denied"
    assert "secret" not in message
