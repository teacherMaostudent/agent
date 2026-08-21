"""Runtime 用户身份与内部工作负载身份的分层边界测试。"""

from types import SimpleNamespace

from platform_sdk.web import _requires_service_key


def _settings() -> SimpleNamespace:
    """返回启用内部服务认证并豁免用户 Agent API 的最小配置。"""
    return SimpleNamespace(
        require_service_auth=True,
        service_auth_exempt_paths=["/api/v1"],
        service_auth_exempt_prefixes=["/api/v1/agent"],
    )


def test_user_agent_api_does_not_require_internal_static_key() -> None:
    """桌面用户 API 应交给 OIDC/OPA，不要求把内部密钥发给客户端。"""
    assert not _requires_service_key(_settings(), "/api/v1/agent/capabilities")
    assert not _requires_service_key(_settings(), "/api/v1/agent/runs/run-1/events")


def test_internal_and_similar_prefix_routes_still_require_service_key() -> None:
    """内部路由及前缀碰撞路由必须继续 fail-closed。"""
    assert _requires_service_key(_settings(), "/api/v1/internal/executor-catalog")
    assert _requires_service_key(_settings(), "/api/v1/agent-evil/ping")
    assert _requires_service_key(_settings(), "/api/v1/internal")


def test_api_landing_uses_exact_exemption_only() -> None:
    """浏览器说明页可以匿名打开，但同一前缀下的业务路由不能继承该豁免。"""
    assert not _requires_service_key(_settings(), "/api/v1")
    assert not _requires_service_key(_settings(), "/api/v1/")
    assert _requires_service_key(_settings(), "/api/v1/private")


def test_health_and_disabled_compatibility_auth_are_not_blocked() -> None:
    """健康探针保持公开；完全关闭兼容认证时不额外要求静态密钥。"""
    assert not _requires_service_key(_settings(), "/api/v1/health/ready")
    disabled = SimpleNamespace(
        require_service_auth=False,
        service_auth_exempt_paths=[],
        service_auth_exempt_prefixes=[],
    )
    assert not _requires_service_key(disabled, "/api/v1/internal/ping")
