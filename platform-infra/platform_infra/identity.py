from __future__ import annotations

from collections.abc import Iterable
from contextlib import suppress
from threading import Lock
from time import monotonic
from typing import Any

import httpx
import jwt
from jwt import PyJWKClient
from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send


class OidcIdentityMiddleware:
    """Validate OIDC JWTs and replace all caller-controlled identity headers.

    Application handlers consume headers for framework compatibility, but they
    are removed and reconstructed only after JWT verification.  Delegated
    tenant headers are accepted solely from an authenticated workload role.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        enabled: bool,
        issuer: str,
        audience: str,
        jwks_url: str = "",
        tenant_claim: str = "tenant_id",
        user_claim: str = "sub",
        roles_claim: str = "roles",
        permissions_claim: str = "permissions",
        public_paths: Iterable[str] = ("/health/live", "/health/ready", "/metrics"),
        trusted_workload_prefixes: Iterable[str] = (),
        workload_roles: Iterable[str] = ("platform-workload",),
    ) -> None:
        """配置 JWT 验证来源、声明映射及受信任工作负载例外。

        ``trusted_workload_prefixes`` 只能用于独立受 mTLS/网络策略保护的端点；其余
        业务请求都会删除调用方伪造的身份 Header 后再写入验证后的声明。
        """
        self.app = app
        self.enabled = enabled
        self.issuer = issuer.rstrip("/")
        self.audience = audience
        self.tenant_claim = tenant_claim
        self.user_claim = user_claim
        self.roles_claim = roles_claim
        self.permissions_claim = permissions_claim
        self.public_paths = tuple(public_paths)
        self.trusted_workload_prefixes = tuple(trusted_workload_prefixes)
        self.workload_roles = frozenset(workload_roles)
        self.jwks = (
            PyJWKClient(jwks_url or f"{self.issuer}/.well-known/jwks.json")
            if enabled
            else None
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """验证 Bearer JWT 并重建下游兼容身份 Header。

        仅带 platform-workload 角色的主体可委托租户；普通用户的 tenant/user 必须取自
        已验证声明。任何验签、受众或必需声明失败一律返回 401。
        """
        if (
            not self.enabled
            or scope["type"] != "http"
            or scope.get("path", "") in self.public_paths
            or any(
                scope.get("path", "").startswith(prefix)
                for prefix in self.trusted_workload_prefixes
            )
        ):
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        authorization = headers.get("authorization", "")
        if not authorization.lower().startswith("bearer "):
            await self._reject(send, "missing bearer token")
            return
        try:
            token = authorization[7:].strip()
            assert self.jwks is not None
            key = self.jwks.get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token,
                key.key,
                algorithms=["RS256", "ES256"],
                audience=self.audience,
                issuer=self.issuer,
                options={"require": ["exp", "iat", "iss", "sub"]},
            )
            roles = _roles(claims.get(self.roles_claim, []))
            # During migration, downstream services may still read X-Permissions;
            # this value is overwritten from verified OIDC claims, never trusted
            # from the caller's original header.
            permissions = _roles(
                claims.get(self.permissions_claim, claims.get("scope", []))
            )
            if self.workload_roles.intersection(roles):
                # Delegated tenant/user context is accepted only from a verified workload.
                # The values are removed and reissued below, so an unauthenticated caller
                # can never establish identity by supplying headers directly.
                tenant = _required_header(headers, "X-Tenant-Id")
                user = headers.get("X-User-Id", "").strip() or _required_claim(
                    claims, self.user_claim
                )
            else:
                tenant = _required_claim(claims, self.tenant_claim)
                user = _required_claim(claims, self.user_claim)
        except (AssertionError, KeyError, ValueError, jwt.PyJWTError) as exc:
            await self._reject(send, f"invalid bearer token: {type(exc).__name__}")
            return

        child_scope = dict(scope)
        child_scope["headers"] = list(scope.get("headers", []))
        mutable = MutableHeaders(scope=child_scope)
        for name in ("X-Tenant-Id", "X-User-Id", "X-Roles", "X-Permissions"):
            with suppress(KeyError):
                del mutable[name]
        mutable.append("X-Tenant-Id", tenant)
        mutable.append("X-User-Id", user)
        mutable.append("X-Roles", ",".join(roles))
        mutable.append("X-Permissions", ",".join(permissions))
        child_scope["auth.claims"] = claims
        await self.app(child_scope, receive, send)

    @staticmethod
    async def _reject(send: Send, detail: str) -> None:
        """以统一 JSON 401 终止 ASGI 调用，避免继续进入任何业务中间件。"""
        response = JSONResponse({"detail": detail}, status_code=401)
        await response({"type": "http"}, lambda: None, send)


def _required_claim(claims: dict[str, Any], name: str) -> str:
    """读取非空字符串声明；不接受隐式类型转换以避免身份歧义。"""
    value = claims.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"required claim is missing: {name}")
    return value.strip()


def _roles(value: Any) -> list[str]:
    """标准化逗号字符串或列表形式的角色/权限声明。"""
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _required_header(headers: Headers, name: str) -> str:
    """读取经过认证工作负载委托的必填 Header，缺失即拒绝。"""
    value = headers.get(name, "").strip()
    if not value:
        raise ValueError(f"verified workload did not provide delegated header: {name}")
    return value


class WorkloadTokenProvider:
    """Cached OAuth2 client-credentials token provider for service-to-service calls."""

    def __init__(
        self,
        *,
        token_url: str,
        client_id: str,
        client_secret: str,
        audience: str = "",
        scope: str = "",
        timeout: float = 5.0,
    ) -> None:
        """配置 client-credentials 凭据并初始化带锁缓存；密钥仅应来自 Secret 挂载。"""
        self.token_url = token_url
        self.client_id = client_id
        self.client_secret = client_secret
        self.audience = audience
        self.scope = scope
        self.timeout = timeout
        self._token = ""
        self._expires_at = 0.0
        self._lock = Lock()

    @property
    def enabled(self) -> bool:
        """仅当三项必需 OAuth 配置齐全时启用令牌注入。"""
        return bool(self.token_url and self.client_id and self.client_secret)

    def authorization_header(self) -> dict[str, str]:
        """返回认证头；未配置身份时返回空值以保留本地开发兼容性。"""
        if not self.enabled:
            return {}
        return {"Authorization": f"Bearer {self.access_token()}"}

    def access_token(self) -> str:
        """在锁内刷新访问令牌，避免并发请求同时冲击身份提供方。"""
        with self._lock:
            if self._token and monotonic() < self._expires_at:
                return self._token
            data = {"grant_type": "client_credentials"}
            if self.audience:
                data["audience"] = self.audience
            if self.scope:
                data["scope"] = self.scope
            response = httpx.post(
                self.token_url,
                data=data,
                auth=(self.client_id, self.client_secret),
                timeout=self.timeout,
            )
            response.raise_for_status()
            payload = response.json()
            token = payload.get("access_token")
            if not isinstance(token, str) or not token:
                raise ValueError("OIDC token endpoint did not return access_token")
            expires_in = max(30, int(payload.get("expires_in", 300)))
            self._token = token
            self._expires_at = monotonic() + max(1, expires_in - 30)
            return token


def build_workload_token_provider(settings: Any) -> WorkloadTokenProvider:
    """从任意服务 Settings 读取统一字段，构造其专属工作负载令牌提供者。"""
    return WorkloadTokenProvider(
        token_url=getattr(settings, "workload_token_url", ""),
        client_id=getattr(settings, "workload_client_id", ""),
        client_secret=getattr(settings, "workload_client_secret", ""),
        audience=getattr(settings, "workload_audience", ""),
        scope=getattr(settings, "workload_scope", ""),
    )
