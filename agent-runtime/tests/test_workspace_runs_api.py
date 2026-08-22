"""Workspace 任务投影的所有权边界测试。"""

from types import SimpleNamespace

from agent_runtime_service.service_api.runtime_api import list_my_runs


class _Store:
    """记录 Workspace 查询条件，避免测试误把前端过滤当成授权实现。"""

    def __init__(self) -> None:
        self.arguments = None

    def list_for_user(self, tenant_id: str, user_id: str, *, limit: int):
        """返回空列表并保存后端实际用于查询的受信任主体。"""
        self.arguments = (tenant_id, user_id, limit)
        return []


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

    assert body == {"items": []}
    assert store.arguments == ("tenant-a", "user-a", 100)
