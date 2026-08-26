"""Authorization Code + PKCE browser session boundary.

The browser never receives the access token after the callback.  A random, HttpOnly cookie maps
to a Redis record owned by this BFF; the verified token is injected into the inner OIDC identity
middleware for every request.  This keeps downstream services on the same JWT trust model while
removing bearer tokens from browser JavaScript and local storage.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
from contextlib import suppress
from typing import Any
from urllib.parse import urlencode

import httpx
import jwt
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from jwt import PyJWKClient
from redis.asyncio import Redis
from starlette.datastructures import MutableHeaders
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from agent_web_bff.config import WebBffSettings


def _b64url(value: bytes) -> str:
    """Encode PKCE material without padding as required by RFC 7636."""
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _safe_return_path(value: str) -> str:
    """Accept only same-origin relative paths so the login endpoint cannot become an open redirect."""
    return value if value.startswith("/") and not value.startswith("//") else "/"


class BrowserSessionStore:
    """Small Redis-backed store for one-time OAuth state and revocable browser sessions."""

    def __init__(self, settings: WebBffSettings) -> None:
        self.settings = settings
        self.redis = Redis.from_url(settings.session_redis_url, decode_responses=True)

    async def save_state(self, state: str, payload: dict[str, Any]) -> None:
        """Persist PKCE verifier and nonce for ten minutes; neither value is placed in a cookie."""
        await self.redis.setex(f"agent-web:oidc-state:{state}", 600, json.dumps(payload))

    async def consume_state(self, state: str) -> dict[str, Any] | None:
        """Atomically consume OAuth state so callback retries cannot create multiple sessions."""
        raw = await self.redis.getdel(f"agent-web:oidc-state:{state}")
        return json.loads(raw) if raw else None

    async def create_session(self, access_token: str, expires_in: int) -> str:
        """Create an opaque session handle bounded by both token and configured session lifetime."""
        session_id = secrets.token_urlsafe(32)
        ttl = min(max(expires_in - 30, 60), self.settings.session_ttl_seconds)
        await self.redis.setex(
            f"agent-web:session:{session_id}", ttl, json.dumps({"access_token": access_token})
        )
        return session_id

    async def access_token(self, session_id: str) -> str:
        """Resolve a live server-side session without extending its expiry on every request."""
        raw = await self.redis.get(f"agent-web:session:{session_id}")
        if not raw:
            return ""
        with suppress(json.JSONDecodeError, TypeError):
            value = json.loads(raw)
            return str(value.get("access_token", ""))
        return ""

    async def revoke(self, session_id: str) -> None:
        """Delete the local session immediately; IdP-wide logout remains provider-specific."""
        if session_id:
            await self.redis.delete(f"agent-web:session:{session_id}")


class BrowserOidcSessionMiddleware:
    """Convert an opaque BFF cookie into an internal Authorization header before JWT validation."""

    def __init__(self, app: ASGIApp, *, settings: WebBffSettings, store: BrowserSessionStore) -> None:
        self.app = app
        self.settings = settings
        self.store = store

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Fail closed for APIs and redirect document navigation when no server session exists."""
        if not self.settings.oidc_enabled or scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        # Do not inject a known-bad legacy token into logout.  This gives a user a deterministic
        # recovery path after IdP claims, signing keys, or audience rules change.
        if path in {"/health/ready", "/auth/login", "/auth/callback", "/auth/logout", "/auth/relogin"}:
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        session_id = request.cookies.get(self.settings.session_cookie_name, "")
        token = await self.store.access_token(session_id)
        if not token:
            if path.startswith("/api/"):
                await JSONResponse({"detail": "browser session is required"}, status_code=401)(
                    scope, receive, send
                )
            else:
                location = "/auth/login?" + urlencode({"return_to": path or "/"})
                await RedirectResponse(location, status_code=303)(scope, receive, send)
            return
        child_scope = dict(scope)
        child_scope["headers"] = list(scope.get("headers", []))
        headers = MutableHeaders(scope=child_scope)
        with suppress(KeyError):
            del headers["Authorization"]
        headers.append("Authorization", f"Bearer {token}")
        await self.app(child_scope, receive, send)


def build_auth_router(settings: WebBffSettings, store: BrowserSessionStore) -> APIRouter:
    """Build the PKCE login/callback/logout endpoints around the shared Redis session store."""
    router = APIRouter(tags=["browser-auth"])

    @router.get("/auth/login")
    async def login(return_to: str = "/") -> RedirectResponse:
        """Start Authorization Code flow with one-time state, nonce and S256 code challenge."""
        if not settings.oidc_enabled:
            return RedirectResponse(_safe_return_path(return_to), status_code=303)
        state = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)
        challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
        await store.save_state(
            state,
            {"nonce": nonce, "verifier": verifier, "return_to": _safe_return_path(return_to)},
        )
        query = urlencode(
            {
                "response_type": "code",
                "client_id": settings.oidc_client_id,
                "redirect_uri": settings.oidc_redirect_uri,
                "scope": settings.oidc_scopes,
                "state": state,
                "nonce": nonce,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
        return RedirectResponse(f"{settings.oidc_authorization_url}?{query}", status_code=303)

    @router.get("/auth/callback")
    async def callback(request: Request, code: str = "", state: str = "") -> RedirectResponse:
        """Consume state, exchange the code, verify ID-token nonce and establish the BFF session."""
        if not code or not state:
            raise HTTPException(status_code=400, detail="OIDC callback is missing code or state")
        pending = await store.consume_state(state)
        if pending is None:
            raise HTTPException(status_code=400, detail="OIDC state is invalid or already consumed")
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": settings.oidc_redirect_uri,
            "client_id": settings.oidc_client_id,
            "code_verifier": pending["verifier"],
        }
        auth = None
        if settings.oidc_client_secret:
            auth = (settings.oidc_client_id, settings.oidc_client_secret)
            data.pop("client_id")
        try:
            async with httpx.AsyncClient(timeout=settings.request_timeout_seconds) as client:
                token_response = await client.post(settings.oidc_token_url, data=data, auth=auth)
            token_response.raise_for_status()
            token_payload = token_response.json()
            access_token = str(token_payload["access_token"])
            id_token = str(token_payload["id_token"])
            jwks = PyJWKClient(settings.oidc_jwks_url or f"{settings.oidc_issuer}/.well-known/jwks.json")
            signing_key = jwks.get_signing_key_from_jwt(id_token)
            claims = jwt.decode(
                id_token,
                signing_key.key,
                algorithms=["RS256", "ES256"],
                audience=settings.oidc_client_id,
                issuer=settings.oidc_issuer.rstrip("/"),
                options={"require": ["exp", "iat", "iss", "sub", "nonce"]},
            )
            if not secrets.compare_digest(str(claims.get("nonce", "")), str(pending["nonce"])):
                raise ValueError("OIDC nonce mismatch")
            expires_in = int(token_payload.get("expires_in", settings.session_ttl_seconds))
        except (httpx.HTTPError, KeyError, TypeError, ValueError, jwt.PyJWTError) as exc:
            raise HTTPException(status_code=502, detail="OIDC token exchange or validation failed") from exc
        session_id = await store.create_session(access_token, expires_in)
        response = RedirectResponse(_safe_return_path(str(pending.get("return_to", "/"))), status_code=303)
        response.set_cookie(
            settings.session_cookie_name,
            session_id,
            max_age=min(expires_in, settings.session_ttl_seconds),
            httponly=True,
            secure=settings.environment.lower() in {"production", "prod"},
            samesite="lax",
            path="/",
        )
        return response

    @router.get("/auth/relogin")
    async def relogin(request: Request) -> RedirectResponse:
        """Clear an unverifiable browser session, then restart the PKCE login redirect.

        This is intentionally limited to cookie revocation: cross-site navigation can at most
        sign a user out, never mutate a business resource or elevate an identity.  Keeping the
        recovery as a server-side redirect also avoids relying on blocked inline scripts/forms
        under the BFF's strict Content Security Policy.
        """
        await store.revoke(request.cookies.get(settings.session_cookie_name, ""))
        response = RedirectResponse("/", status_code=303)
        response.delete_cookie(settings.session_cookie_name, path="/")
        return response

    @router.post("/auth/logout", status_code=204)
    async def logout(request: Request, return_to: str = "") -> Response:
        """Revoke the local session and optionally return the browser to a fresh login flow."""
        await store.revoke(request.cookies.get(settings.session_cookie_name, ""))
        response: Response = (
            RedirectResponse(_safe_return_path(return_to), status_code=303)
            if return_to
            else Response(status_code=204)
        )
        response.delete_cookie(settings.session_cookie_name, path="/")
        return response

    return router
