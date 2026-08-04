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
    """Verifies caller identity and reissues trusted internal identity headers.

    Application handlers consume headers for framework compatibility, but
    those headers are removed and reconstructed only after JWT verification.
    """
    """Validate OIDC JWTs and replace all caller-controlled identity headers."""

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
        response = JSONResponse({"detail": detail}, status_code=401)
        await response({"type": "http"}, lambda: None, send)


def _required_claim(claims: dict[str, Any], name: str) -> str:
    value = claims.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"required claim is missing: {name}")
    return value.strip()


def _roles(value: Any) -> list[str]:
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _required_header(headers: Headers, name: str) -> str:
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
        return bool(self.token_url and self.client_id and self.client_secret)

    def authorization_header(self) -> dict[str, str]:
        if not self.enabled:
            return {}
        return {"Authorization": f"Bearer {self.access_token()}"}

    def access_token(self) -> str:
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
    return WorkloadTokenProvider(
        token_url=getattr(settings, "workload_token_url", ""),
        client_id=getattr(settings, "workload_client_id", ""),
        client_secret=getattr(settings, "workload_client_secret", ""),
        audience=getattr(settings, "workload_audience", ""),
        scope=getattr(settings, "workload_scope", ""),
    )
