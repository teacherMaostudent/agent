"""HTTP dependency boundaries for management and Runtime callers."""

from __future__ import annotations

import secrets
from uuid import uuid4

from fastapi import Header, HTTPException, Request, status

from app.container import AppContainer
from app.domain.models import Identity


def get_container(request: Request) -> AppContainer:
    """获取应用启动时创建的依赖容器，不在单个请求内重复连接基础设施。"""
    return request.app.state.container


def get_trace_id(request: Request) -> str:
    """取得中间件已建立的 Trace ID；测试或内部调用缺失时才惰性补建。"""
    trace_id = getattr(request.state, "trace_id", None)
    if trace_id:
        return str(trace_id)
    trace_id = f"trace_{uuid4().hex}"
    request.state.trace_id = trace_id
    return trace_id


def management_identity(
    request: Request,
    x_tenant_id: str = Header(min_length=1, alias="X-Tenant-Id"),
    x_user_id: str = Header(min_length=1, alias="X-User-Id"),
    x_roles: str = Header(default="", alias="X-Roles"),
    x_admin_key: str | None = Header(default=None, alias="X-Control-Plane-Admin-Key"),
) -> Identity:
    """校验管理身份是否具备变更发布配置所需的角色与管理员凭据。

    Require a management role before allowing mutable release operations.
    """
    identity = Identity(tenant_id=x_tenant_id, user_id=x_user_id, roles=x_roles)
    container = get_container(request)
    expected_key = container.settings.admin_api_key
    if expected_key and not secrets.compare_digest(x_admin_key or "", expected_key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "invalid_admin_key", "message": "Admin credential is invalid."},
        )
    if container.settings.enforce_admin_role and "agent-admin" not in identity.roles:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "forbidden", "message": "The agent-admin role is required."},
        )
    return identity


def runtime_identity(
    request: Request,
    x_tenant_id: str = Header(min_length=1, alias="X-Tenant-Id"),
    x_user_id: str = Header(default="agent-runtime", alias="X-User-Id"),
    x_runtime_key: str | None = Header(default=None, alias="X-Runtime-Key"),
) -> Identity:
    """处理 runtime_identity 对应的当前组件内部业务步骤。



    Authenticate Runtime resolution without granting authoring permissions.
    """
    container = get_container(request)
    expected_key = container.settings.runtime_api_key
    if expected_key and x_runtime_key != expected_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "invalid_runtime_key", "message": "Runtime API key is invalid."},
        )
    return Identity(tenant_id=x_tenant_id, user_id=x_user_id, roles={"agent-runtime"})
