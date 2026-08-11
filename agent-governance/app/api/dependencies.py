from __future__ import annotations

import secrets
from uuid import uuid4

from fastapi import Header, HTTPException, Request, status

from app.container import AppContainer
from app.domain.models import Identity


def get_container(request: Request) -> AppContainer:
    """取得应用级依赖容器，避免请求路径临时创建仓储或模型客户端。"""
    return request.app.state.container


def auditor_identity(
    request: Request,
    x_tenant_id: str = Header(min_length=1, alias="X-Tenant-Id"),
    x_user_id: str = Header(min_length=1, alias="X-User-Id"),
    x_roles: str = Header(default="", alias="X-Roles"),
    x_auditor_key: str | None = Header(default=None, alias="X-Governance-Auditor-Key"),
) -> Identity:
    """校验审计员凭据和角色，并将请求身份限制在 Header 声明的租户内。"""
    identity = Identity(tenant_id=x_tenant_id, user_id=x_user_id, roles=x_roles)
    expected_key = get_container(request).settings.auditor_api_key
    if expected_key and not secrets.compare_digest(x_auditor_key or "", expected_key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="auditor credential is invalid",
        )
    if (
        get_container(request).settings.enforce_auditor_role
        and "governance-auditor" not in identity.roles
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="governance-auditor role is required"
        )
    return identity


def validate_event_key(
    request: Request,
    x_governance_event_key: str | None = Header(default=None, alias="X-Governance-Event-Key"),
) -> None:
    """验证跨服务事件写入密钥；OIDC 身份不等同于拥有审计写入权限。"""
    expected_key = get_container(request).settings.event_ingestion_key
    if expected_key and x_governance_event_key != expected_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="event ingestion key is invalid"
        )


def get_trace_id(request: Request) -> str:
    """保留上游 Trace ID 或生成新值，使异步审计事件能够关联原请求。"""
    return request.headers.get("X-Trace-Id") or f"trace_{uuid4().hex}"
