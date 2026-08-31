"""Workspace 任务投影的所有权边界测试。"""

from types import SimpleNamespace

import httpx

from agent_runtime_service.service_api.runtime_api import list_my_runs


def test_run_audit_read_preserves_workload_identity_mtls_and_cursor(monkeypatch) -> None:
    """单 Run 授权之后，审计读取也必须携带服务身份、证书和有界分页参数。"""
    from agent_runtime_service.service_api import runtime_api

    calls = []

    def get(url, **kwargs):
        """记录出站边界，不在单元测试读取真实证书。"""
        calls.append((url, kwargs))
        return httpx.Response(200, json={"items": [], "next_cursor": 12},
                              request=httpx.Request("GET", url))

    monkeypatch.setattr(runtime_api.httpx, "get", get)
    container = SimpleNamespace(
        settings=SimpleNamespace(oidc_enabled=False, governance_base_url="https://governance",
                                 governance_event_key="test-key", service_http_timeout=5),
        workload_identity=SimpleNamespace(authorization_header=lambda: {"Authorization": "Bearer workload-test"}),
        run_store=SimpleNamespace(get=lambda *_: SimpleNamespace(user_id="owner")),
        _mtls_options=lambda: {"verify": "test-ca", "cert": ("test-cert", "test-key")},
    )
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(container=container)), scope={})
    result = runtime_api.get_run_audit_events(
        "run-test", request, x_tenant_id="tenant-a", x_user_id="owner", x_permissions="",
        after_sequence=10, limit=5000,
    )
    kwargs = calls[0][1]
    assert kwargs["headers"]["Authorization"] == "Bearer workload-test"
    assert kwargs["verify"] == "test-ca"
    assert kwargs["cert"] == ("test-cert", "test-key")
    assert kwargs["params"] == {"after_sequence": 10, "limit": 1000}
    assert result["next_cursor"] == 12 and result["status"] == "available"


class _Store:
    """记录 Workspace 查询条件，避免测试误把前端过滤当成授权实现。"""

    def __init__(self) -> None:
        self.arguments = None

    def list_for_user(self, tenant_id: str, user_id: str, *, limit: int, offset: int):
        """返回空列表并保存后端实际用于查询的受信任主体。"""
        self.arguments = (tenant_id, user_id, limit, offset)
        return []

    def count_for_user(self, tenant_id: str, user_id: str):
        return 0

    def list_for_tenant(self, tenant_id: str, *, limit: int, offset: int):
        """Record the deliberately separate administrator-read query."""
        self.arguments = (tenant_id, "tenant-admin", limit, offset)
        return []

    def count_for_tenant(self, tenant_id: str):
        return 0


def test_workspace_list_is_bound_to_verified_identity() -> None:
    """“我的任务”不接收目标用户参数，只查询经身份中间件重建的当前主体。"""
    store = _Store()
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                container=SimpleNamespace(
                    run_store=store,
                    settings=SimpleNamespace(oidc_enabled=False),
                )
            )
        ),
        scope={},
    )

    body = list_my_runs(
        request,
        limit=500,
        x_tenant_id="tenant-a",
        x_user_id="user-a",
    )

    assert body == {"scope": "owned-or-shared", "offset": 0, "limit": 100, "total_items": 0, "items": []}
    assert store.arguments == ("tenant-a", "user-a", 100, 0)


def test_workspace_tenant_read_requires_explicit_permission() -> None:
    """A supervisor capability changes only the query scope, never a user-id request parameter."""
    store = _Store()
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                container=SimpleNamespace(
                    run_store=store, settings=SimpleNamespace(oidc_enabled=False)
                )
            )
        ),
        scope={},
    )

    body = list_my_runs(
        request,
        x_tenant_id="tenant-a",
        x_user_id="admin-a",
        x_permissions="run:tenant:read",
    )

    assert body == {"scope": "tenant-admin", "offset": 0, "limit": 30, "total_items": 0, "items": []}
    assert store.arguments == ("tenant-a", "tenant-admin", 30, 0)


def test_workspace_list_uses_bounded_offset_for_history_pages() -> None:
    """The page control may move through history but cannot turn into an unbounded database scan."""
    store = _Store()
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(container=SimpleNamespace(
            run_store=store, settings=SimpleNamespace(oidc_enabled=False)
        ))), scope={},
    )

    body = list_my_runs(request, limit=8, offset=999_999, x_tenant_id="tenant-a", x_user_id="user-a")

    assert body["offset"] == 10_000
    assert body["limit"] == 8
    assert store.arguments == ("tenant-a", "user-a", 8, 10_000)
