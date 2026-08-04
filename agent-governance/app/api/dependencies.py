from __future__ import annotations

import secrets
from uuid import uuid4

from fastapi import Header, HTTPException, Request, status

from app.container import AppContainer
from app.domain.models import Identity


def get_container(request: Request) -> AppContainer:
    return request.app.state.container


def auditor_identity(
    request: Request,
    x_tenant_id: str = Header(min_length=1, alias="X-Tenant-Id"),
    x_user_id: str = Header(min_length=1, alias="X-User-Id"),
    x_roles: str = Header(default="", alias="X-Roles"),
    x_auditor_key: str | None = Header(default=None, alias="X-Governance-Auditor-Key"),
) -> Identity:
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
    expected_key = get_container(request).settings.event_ingestion_key
    if expected_key and x_governance_event_key != expected_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="event ingestion key is invalid"
        )


def get_trace_id(request: Request) -> str:
    return request.headers.get("X-Trace-Id") or f"trace_{uuid4().hex}"
