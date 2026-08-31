"""Browser identity and redirect boundary regression tests."""

import asyncio
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request
from starlette.responses import JSONResponse

from agent_web_bff.browser_oidc import (
    PUBLIC_OIDC_PATHS,
    BrowserOidcSessionMiddleware,
    BrowserSessionStore,
    _safe_return_path,
    build_auth_router,
)
from agent_web_bff.config import WebBffSettings
from agent_web_bff.main import BrowserBoundaryMiddleware, app as web_bff_app


def test_oidc_return_path_rejects_external_redirects() -> None:
    """OAuth return_to accepts only a same-origin absolute path."""
    assert _safe_return_path("/review?run=1") == "/review?run=1"
    assert _safe_return_path("//evil.example/path") == "/"
    assert _safe_return_path("https://evil.example/path") == "/"


@pytest.mark.parametrize("path", ["/\\evil.example", "/\n/evil.example", "/\r/evil.example"])
def test_return_path_rejects_browser_url_normalization(path) -> None:
    """浏览器归一化不能把回跳地址转换为外部站点。"""
    assert _safe_return_path(path) == "/"


def test_browser_session_expires_before_token(monkeypatch) -> None:
    """短期 Token 的服务端 Session 不得因为最小 TTL 被延长。"""
    store = BrowserSessionStore(WebBffSettings(session_redis_url="redis://localhost:6379/2"))
    redis = AsyncMock()
    monkeypatch.setattr(store, "redis", redis)
    asyncio.run(store.create_session("test-token", 45))
    assert redis.setex.call_args.args[1] == 15
    with pytest.raises(ValueError, match="too short"):
        asyncio.run(store.create_session("test-token", 30))
    assert redis.setex.call_count == 1


def test_expired_session_redirects_documents_but_not_api_or_static_assets() -> None:
    """精确区分页面导航、匿名 API 与辅助资源，避免重复启动授权。"""
    async def inner(scope, receive, send):
        await JSONResponse({"authorization": Request(scope).headers.get("authorization")})(scope, receive, send)

    store = AsyncMock()
    store.access_token.return_value = ""
    client = TestClient(BrowserOidcSessionMiddleware(
        inner, settings=WebBffSettings(oidc_enabled=True), store=store,
    ))
    assert client.get("/", follow_redirects=False).status_code == 303
    assert client.get("/api/session", follow_redirects=False).status_code == 401
    for path in PUBLIC_OIDC_PATHS:
        assert client.get(path, follow_redirects=False).status_code == 200
    store.access_token.return_value = "verified-server-token"
    response = client.get("/api/session", headers={"Authorization": "Bearer caller-supplied"})
    assert response.json()["authorization"] == "Bearer verified-server-token"


def test_production_browser_bff_requires_complete_pkce_session_configuration() -> None:
    """Production cannot silently fall back to caller supplied identity headers."""
    with pytest.raises(ValueError, match="OIDC issuer"):
        WebBffSettings(environment="production")


def test_production_redirect_must_use_public_origin() -> None:
    """An IdP callback on another origin would leak the authorization code."""
    with pytest.raises(ValueError, match="REDIRECT_URI"):
        WebBffSettings(
            environment="production",
            oidc_enabled=True,
            oidc_issuer="https://idp.example",
            oidc_authorization_url="https://idp.example/authorize",
            oidc_token_url="https://idp.example/token",
            oidc_end_session_url="https://idp.example/logout",
            oidc_client_id="agent-web",
            oidc_redirect_uri="https://other.example/auth/callback",
            session_redis_url="redis://redis:6379/2",
            public_origin="https://agent.example",
            mtls_enabled=True,
            mtls_ca_file="/secrets/ca.pem",
            mtls_cert_file="/secrets/web.crt",
            mtls_key_file="/secrets/web.key",
        )


def test_static_shell_requires_cache_revalidation() -> None:
    """稳定文件名的前端资源必须重新验证，防止容器更新后继续运行旧 UI。"""
    async def inner(scope, receive, send):
        await JSONResponse({"path": scope["path"]})(scope, receive, send)

    client = TestClient(BrowserBoundaryMiddleware(inner))
    for path in ("/", "/index.html", "/app.js", "/styles.css"):
        assert client.get(path).headers["cache-control"] == "no-cache, max-age=0, must-revalidate"
    assert "cache-control" not in client.get("/api/session").headers


def test_switch_account_revokes_session_and_forces_fresh_login() -> None:
    """切换账号必须撤销本地会话并要求 IdP 重新显示凭据登录。"""
    store = AsyncMock()
    app = FastAPI()
    app.include_router(build_auth_router(WebBffSettings(oidc_enabled=True), store))
    client = TestClient(app)
    client.cookies.set("agent_web_session", "session-a")
    response = client.get("/auth/switch-account", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/auth/login?return_to=/&prompt=login"
    store.revoke.assert_awaited_once_with("session-a")


def test_signout_revokes_bff_and_redirects_to_idp_logout() -> None:
    """退出登录必须同时结束 IdP SSO，不能自动回到原账号。"""
    store = AsyncMock()
    app = FastAPI()
    app.include_router(build_auth_router(WebBffSettings(
        oidc_enabled=True,
        oidc_client_id="agent-web-bff",
        oidc_end_session_url="https://idp.example/logout",
        public_origin="https://agent.example",
    ), store))
    client = TestClient(app)
    client.cookies.set("agent_web_session", "session-b")
    response = client.get("/auth/signout", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == (
        "https://idp.example/logout?client_id=agent-web-bff&"
        "post_logout_redirect_uri=https%3A%2F%2Fagent.example%2F"
    )
    store.revoke.assert_awaited_once_with("session-b")


def test_local_compose_signout_has_a_real_neutral_redirect() -> None:
    """本地 Compose 也必须有可见、可执行的结束身份入口，不能将退出按钮隐藏后返回 404。"""
    response = TestClient(web_bff_app).get("/auth/signout", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/?local_signed_out=1"


def test_session_marks_local_identity_directory_as_unavailable() -> None:
    """Header compatibility mode must not masquerade as an editable user directory."""
    response = TestClient(web_bff_app).get(
        "/api/session",
        headers={"X-Tenant-Id": "demo", "X-User-Id": "local-admin"},
    )
    assert response.status_code == 200
    assert response.json()["identity_management_available"] is False
