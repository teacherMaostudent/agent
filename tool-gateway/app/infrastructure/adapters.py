"""Outbound adapter implementations behind the Tool Gateway security boundary."""

from __future__ import annotations

import asyncio
import os
import re
from collections.abc import Awaitable, Callable
from typing import Any, Protocol
from urllib.parse import quote

import httpx

from app.domain.errors import ToolUpstreamError, ToolValidationError, UnsafeEndpointError
from app.domain.models import HttpTransport, InvocationContext, McpTransport
from app.infrastructure.security import validate_outbound_url

_PATH_ARGUMENT = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


class ToolAdapter(Protocol):
    """Execute a catalogued tool after Gateway authorization has already passed."""

    async def execute(self, arguments: dict[str, Any], context: InvocationContext) -> Any:
        """把已授权且通过 Schema
        校验的参数转换为下游协议；只允许预注册主机，并统一映射超时和上游错误。

        Execute the already-authorized operation without adding a second policy decision.
        """
        ...

    async def close(self) -> None:
        """关闭适配器持有的 HTTP/MCP 会话；该操作不撤销已提交的业务副作用。

        Release transport resources owned by the concrete adapter during shutdown.
        """
        ...


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
        """冻结适配器端点、凭据引用、超时和允许主机配置；运行期不接受模型修改这些安全参数。"""
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
        """把已授权且通过 Schema
        校验的参数转换为下游协议；只允许预注册主机，并统一映射超时和上游错误。
        """
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
            if response.status_code in {400, 422}:
                # 下游契约明确拒绝了模型参数时, 返回可恢复的参数错误且禁止重试。
                # 只提取受限 JSON 字段, 不回传任意响应正文或内部堆栈。
                raise ToolValidationError(_validation_message(response))
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
        """关闭适配器持有的 HTTP/MCP 会话；该操作不撤销已提交的业务副作用。"""
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
        """冻结适配器端点、凭据引用、超时和允许主机配置；运行期不接受模型修改这些安全参数。"""
        self.config = config
        self.allow_private_networks = allow_private_networks
        validate_outbound_url(
            config.server_url,
            config.allowed_hosts,
            allow_private_networks=allow_private_networks,
            resolve_dns=False,
        )

    async def execute(self, arguments: dict[str, Any], context: InvocationContext) -> Any:
        """把已授权且通过 Schema
        校验的参数转换为下游协议；只允许预注册主机，并统一映射超时和上游错误。
        """
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
        """关闭适配器持有的 HTTP/MCP 会话；该操作不撤销已提交的业务副作用。"""
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
        """冻结适配器端点、凭据引用、超时和允许主机配置；运行期不接受模型修改这些安全参数。"""
        self.handler = handler

    async def execute(self, arguments: dict[str, Any], context: InvocationContext) -> Any:
        """把已授权且通过 Schema
        校验的参数转换为下游协议；只允许预注册主机，并统一映射超时和上游错误。
        """
        result = self.handler(arguments, context)
        if isinstance(result, Awaitable):
            return await result
        return result

    async def close(self) -> None:
        """关闭适配器持有的 HTTP/MCP 会话；该操作不撤销已提交的业务副作用。"""
        return None


def build_http_adapter(
    config: HttpTransport,
    *,
    allow_private_networks: bool,
    max_response_bytes: int,
    client_options: dict[str, Any] | None = None,
) -> HttpToolAdapter:
    """从已验证 ToolSpec 构建 HTTP 适配器，并把凭据解析留在受控配置边界。

    Create the only HTTP adapter type after its outbound policy has been validated.
    """
    return HttpToolAdapter(
        config,
        allow_private_networks=allow_private_networks,
        max_response_bytes=max_response_bytes,
        client_options=client_options,
    )


def _render_url(template: str, arguments: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """将声明式路径参数渲染到固定 URL 模板，返回未消费参数作为请求体。

    Substitute allow-listed path placeholders with escaped scalars and retain other arguments.
    """
    consumed: set[str] = set()

    def replace(match: re.Match[str]) -> str:
        """将 URL
        模板占位符替换为已校验参数并进行转义，防止参数改变协议、主机或路径边界。

        Reject missing or complex path values before they can change an outbound URL.
        """
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
    """构建下游请求头并传播可信 Trace；禁止参数覆盖认证、Host 或内部安全头。

    Build traceable upstream headers without placing raw approval data in the URL.
    """
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
    """去重并关闭全部适配器；单个关闭失败不会跳过其余资源清理。

    Release or remove owned state without bypassing cleanup rules.
    """
    await asyncio.gather(*(adapter.close() for adapter in adapters))


def _validation_message(response: httpx.Response) -> str:
    """从受信下游的 4xx JSON 中提取短错误提示，拒绝泄露 HTML 或任意正文。"""
    fallback = f"tool arguments were rejected by upstream (HTTP {response.status_code})"
    try:
        payload = response.json()
    except ValueError:
        return fallback
    detail = payload.get("detail") if isinstance(payload, dict) else None
    if isinstance(detail, str):
        return detail[:500]
    if isinstance(detail, dict):
        message = detail.get("message")
        hint = detail.get("hint")
        parts = [item.strip() for item in (message, hint) if isinstance(item, str) and item.strip()]
        if parts:
            return " ".join(parts)[:500]
    return fallback
