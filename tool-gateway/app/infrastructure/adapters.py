"""Outbound adapter implementations behind the Tool Gateway security boundary."""

from __future__ import annotations

import asyncio
import os
import re
from collections.abc import Awaitable, Callable
from typing import Any, Protocol
from urllib.parse import quote

import httpx

from app.domain.errors import ToolUpstreamError, UnsafeEndpointError
from app.domain.models import HttpTransport, InvocationContext, McpTransport
from app.infrastructure.security import validate_outbound_url

_PATH_ARGUMENT = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


class ToolAdapter(Protocol):
    """Execute a catalogued tool after Gateway authorization has already passed."""
    async def execute(self, arguments: dict[str, Any], context: InvocationContext) -> Any: ...

    async def close(self) -> None: ...


class HttpToolAdapter:
    """HTTP adapter that validates outbound destinations against the tool manifest."""
    def __init__(
        self,
        config: HttpTransport,
        *,
        allow_private_networks: bool,
        max_response_bytes: int,
        client: httpx.AsyncClient | None = None,
        client_options: dict[str, Any] | None = None,
    ) -> None:
        """Initialize HttpToolAdapter dependencies and local state."""
        self.config = config
        self.allow_private_networks = allow_private_networks
        self.max_response_bytes = max_response_bytes
        self.client = client or httpx.AsyncClient(follow_redirects=False, **(client_options or {}))
        self._owns_client = client is None
        validate_outbound_url(
            config.url,
            config.allowed_hosts,
            allow_private_networks=allow_private_networks,
            resolve_dns=False,
        )

    async def execute(self, arguments: dict[str, Any], context: InvocationContext) -> Any:
        """Perform execute within the HttpToolAdapter ownership boundary."""
        url, remaining = _render_url(self.config.url, arguments)
        validate_outbound_url(
            url,
            self.config.allowed_hosts,
            allow_private_networks=self.allow_private_networks,
        )
        headers = _build_headers(
            self.config.static_headers,
            self.config.auth_header,
            self.config.auth_env,
            context,
        )
        request_kwargs: dict[str, Any] = {"headers": headers}
        if self.config.argument_location == "query" or self.config.method == "GET":
            request_kwargs["params"] = remaining
        else:
            request_kwargs["json"] = remaining
        try:
            response = await self.client.request(self.config.method, url, **request_kwargs)
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise ToolUpstreamError(
                f"tool upstream transport failed: {type(exc).__name__}",
                retryable=True,
            ) from exc
        if response.is_redirect:
            raise ToolUpstreamError("tool upstream redirects are disabled", retryable=False)
        if response.status_code >= 400:
            retryable = response.status_code in {408, 425, 429} or response.status_code >= 500
            raise ToolUpstreamError(
                f"tool upstream returned HTTP {response.status_code}",
                retryable=retryable,
                details={"status_code": response.status_code},
            )
        content = response.content
        if len(content) > self.max_response_bytes:
            raise ToolUpstreamError("tool upstream response exceeds the configured size limit")
        if not content:
            return None
        content_type = response.headers.get("content-type", "")
        if "json" in content_type:
            try:
                return response.json()
            except ValueError as exc:
                raise ToolUpstreamError("tool upstream returned invalid JSON") from exc
        return response.text

    async def close(self) -> None:
        """Perform close within the HttpToolAdapter ownership boundary."""
        if self._owns_client:
            await self.client.aclose()


class McpToolAdapter:
    """MCP adapter with explicit server and method bindings from the catalog."""
    """Official MCP SDK client using the production Streamable HTTP transport."""

    def __init__(
        self,
        config: McpTransport,
        *,
        allow_private_networks: bool,
    ) -> None:
        """Initialize McpToolAdapter dependencies and local state."""
        self.config = config
        self.allow_private_networks = allow_private_networks
        validate_outbound_url(
            config.server_url,
            config.allowed_hosts,
            allow_private_networks=allow_private_networks,
            resolve_dns=False,
        )

    async def execute(self, arguments: dict[str, Any], context: InvocationContext) -> Any:
        """Perform execute within the McpToolAdapter ownership boundary."""
        validate_outbound_url(
            self.config.server_url,
            self.config.allowed_hosts,
            allow_private_networks=self.allow_private_networks,
        )
        headers = _build_headers(
            self.config.static_headers,
            self.config.auth_header,
            self.config.auth_env,
            context,
        )
        try:
            from mcp import ClientSession
            from mcp.client.streamable_http import streamable_http_client

            async with (
                httpx.AsyncClient(headers=headers, follow_redirects=False) as http_client,
                streamable_http_client(
                    self.config.server_url,
                    http_client=http_client,
                ) as (read_stream, write_stream, _),
                ClientSession(read_stream, write_stream) as session,
            ):
                await session.initialize()
                result = await session.call_tool(
                    self.config.remote_tool_name,
                    arguments=arguments,
                )
        except (UnsafeEndpointError, ToolUpstreamError):
            raise
        except Exception as exc:
            raise ToolUpstreamError(
                f"MCP tool call failed: {type(exc).__name__}",
                retryable=True,
            ) from exc
        if result.isError:
            raise ToolUpstreamError("MCP server returned a tool error", retryable=False)
        if result.structuredContent is not None:
            return result.structuredContent
        return [item.model_dump(mode="json") for item in result.content]

    async def close(self) -> None:
        """Perform close within the McpToolAdapter ownership boundary."""
        return None


class CallableToolAdapter:
    """In-process adapter used by tests and deterministic local extensions."""

    def __init__(
        self,
        handler: Callable[
            [dict[str, Any], InvocationContext],
            Any | Awaitable[Any],
        ],
    ) -> None:
        """Initialize CallableToolAdapter dependencies and local state."""
        self.handler = handler

    async def execute(self, arguments: dict[str, Any], context: InvocationContext) -> Any:
        """Perform execute within the CallableToolAdapter ownership boundary."""
        result = self.handler(arguments, context)
        if isinstance(result, Awaitable):
            return await result
        return result

    async def close(self) -> None:
        """Perform close within the CallableToolAdapter ownership boundary."""
        return None


def build_http_adapter(
    config: HttpTransport,
    *,
    allow_private_networks: bool,
    max_response_bytes: int,
    client_options: dict[str, Any] | None = None,
) -> HttpToolAdapter:
    """Perform build http adapter within the module ownership boundary."""
    return HttpToolAdapter(
        config,
        allow_private_networks=allow_private_networks,
        max_response_bytes=max_response_bytes,
        client_options=client_options,
    )


def _render_url(template: str, arguments: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Internal helper for module; preserve its caller-facing invariant."""
    consumed: set[str] = set()

    def replace(match: re.Match[str]) -> str:
        """Perform replace within the module ownership boundary."""
        name = match.group(1)
        if name not in arguments:
            raise ToolUpstreamError(f"missing URL template argument: {name}")
        value = arguments[name]
        if not isinstance(value, str | int):
            raise ToolUpstreamError(f"URL template argument must be scalar: {name}")
        consumed.add(name)
        return quote(str(value), safe="")

    rendered = _PATH_ARGUMENT.sub(replace, template)
    return rendered, {key: value for key, value in arguments.items() if key not in consumed}


def _build_headers(
    static_headers: dict[str, str],
    auth_header: str | None,
    auth_env: str | None,
    context: InvocationContext,
) -> dict[str, str]:
    """Internal helper for module; preserve its caller-facing invariant."""
    headers = {
        **static_headers,
        "X-Tenant-Id": context.tenant_id,
        "X-User-Id": context.user_id,
        "X-Request-Id": context.request_id,
    }
    if auth_header:
        if not auth_env:
            raise UnsafeEndpointError("auth_header requires auth_env")
        secret = os.getenv(auth_env, "")
        if not secret:
            raise UnsafeEndpointError(f"required tool credential is not configured: {auth_env}")
        headers[auth_header] = secret
    return headers


async def close_adapters(adapters: list[ToolAdapter]) -> None:
    """Release or remove owned state without bypassing cleanup rules."""
    await asyncio.gather(*(adapter.close() for adapter in adapters))
