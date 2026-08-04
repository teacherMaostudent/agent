"""Shared fail-closed Open Policy Agent authorization integration."""

from __future__ import annotations

from typing import Any

import httpx
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send


class OpaAuthorizer:
    """Ask OPA for a policy decision; transport errors deny protected actions."""
    """Fail-closed client for a locally replicated OPA policy decision."""

    def __init__(self, base_url: str, decision_path: str, timeout: float = 2.0) -> None:
        self.url = f"{base_url.rstrip('/')}/v1/data/{decision_path.strip('/')}"
        self.timeout = timeout

    def authorize(self, input_document: dict[str, Any]) -> dict[str, Any]:
        response = httpx.post(
            self.url,
            json={"input": input_document},
            timeout=self.timeout,
        )
        response.raise_for_status()
        result = response.json().get("result", False)
        if isinstance(result, bool):
            allowed, decision = result, {"allow": result}
        elif isinstance(result, dict):
            allowed, decision = result.get("allow") is True, result
        else:
            allowed, decision = False, {"allow": False}
        if not allowed:
            raise PermissionError(str(decision.get("reason", "OPA policy denied request")))
        return decision


class OpaAuthorizationMiddleware:
    """Apply OPA after identity middleware has verified caller claims."""
    def __init__(
        self,
        app: ASGIApp,
        *,
        enabled: bool,
        base_url: str,
        decision_path: str,
        public_paths: tuple[str, ...] = (),
        timeout: float = 2.0,
    ) -> None:
        self.app = app
        self.enabled = enabled
        self.url = f"{base_url.rstrip('/')}/v1/data/{decision_path.strip('/')}"
        self.public_paths = public_paths
        self.timeout = timeout

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            not self.enabled
            or scope["type"] != "http"
            or scope.get("path", "") in self.public_paths
        ):
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        input_document = {
            "subject": {
                "tenant_id": headers.get("X-Tenant-Id", ""),
                "user_id": headers.get("X-User-Id", ""),
                "roles": [
                    role.strip()
                    for role in headers.get("X-Roles", "").split(",")
                    if role.strip()
                ],
            },
            "request": {
                "method": scope.get("method", ""),
                "path": scope.get("path", ""),
            },
        }
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(self.url, json={"input": input_document})
                response.raise_for_status()
                result = response.json().get("result", False)
            allowed = result is True or (
                isinstance(result, dict) and result.get("allow") is True
            )
        except httpx.HTTPError:
            allowed = False
        if not allowed:
            response = JSONResponse({"detail": "OPA policy denied request"}, status_code=403)
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)
