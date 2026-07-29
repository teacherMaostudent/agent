from __future__ import annotations

import httpx
import pytest

from app.domain.errors import ToolUpstreamError
from app.domain.models import HttpTransport, InvocationContext
from app.infrastructure.adapters import HttpToolAdapter


@pytest.mark.asyncio
async def test_http_adapter_encodes_path_and_forwards_trusted_identity() -> None:
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.raw_path
        captured["query"] = dict(request.url.params)
        captured["tenant"] = request.headers["X-Tenant-Id"]
        captured["user"] = request.headers["X-User-Id"]
        return httpx.Response(200, json={"ok": True})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = HttpToolAdapter(
        HttpTransport(
            url="https://example.com/documents/{document_id}",
            method="GET",
            argument_location="query",
            allowed_hosts=["example.com"],
        ),
        allow_private_networks=False,
        max_response_bytes=10_000,
        client=client,
    )
    context = InvocationContext(
        tenant_id="tenant-a",
        user_id="user-a",
        request_id="request-a",
    )

    result = await adapter.execute(
        {"document_id": "folder/doc 1", "include_text": True},
        context,
    )

    assert result == {"ok": True}
    assert captured["path"] == b"/documents/folder%2Fdoc%201?include_text=true"
    assert captured["query"] == {"include_text": "true"}
    assert captured["tenant"] == "tenant-a"
    assert captured["user"] == "user-a"
    await client.aclose()


@pytest.mark.asyncio
async def test_http_adapter_does_not_follow_redirects() -> None:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                302,
                headers={"Location": "http://127.0.0.1/internal"},
            )
        )
    )
    adapter = HttpToolAdapter(
        HttpTransport(
            url="https://example.com/tool",
            method="POST",
            allowed_hosts=["example.com"],
        ),
        allow_private_networks=False,
        max_response_bytes=10_000,
        client=client,
    )
    context = InvocationContext(
        tenant_id="tenant-a",
        user_id="user-a",
        request_id="request-a",
    )

    with pytest.raises(ToolUpstreamError, match="redirects are disabled"):
        await adapter.execute({}, context)

    await client.aclose()
