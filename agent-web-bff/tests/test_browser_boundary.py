"""Browser identity and redirect boundary regression tests."""

import pytest

from agent_web_bff.browser_oidc import _safe_return_path
from agent_web_bff.config import WebBffSettings


def test_oidc_return_path_rejects_external_redirects() -> None:
    """OAuth return_to accepts only a same-origin absolute path."""
    assert _safe_return_path("/review?run=1") == "/review?run=1"
    assert _safe_return_path("//evil.example/path") == "/"
    assert _safe_return_path("https://evil.example/path") == "/"


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
            oidc_client_id="agent-web",
            oidc_redirect_uri="https://other.example/auth/callback",
            session_redis_url="redis://redis:6379/2",
            public_origin="https://agent.example",
            mtls_enabled=True,
            mtls_ca_file="/secrets/ca.pem",
            mtls_cert_file="/secrets/web.crt",
            mtls_key_file="/secrets/web.key",
        )
