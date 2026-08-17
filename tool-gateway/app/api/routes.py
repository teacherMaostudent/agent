"""Tool Gateway HTTP endpoints; invocation semantics remain in the application service."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, Query, Request, status

from app.domain.models import (
    ApprovalDecision,
    ApprovalRecord,
    AuditPage,
    InvocationContext,
    InvocationRequest,
    InvocationResponse,
    InvocationStatus,
    ToolExecutionState,
    ToolManifest,
)

router = APIRouter()


def _context(
    x_tenant_id: str = Header(min_length=1, max_length=200, alias="X-Tenant-Id"),
    x_user_id: str = Header(min_length=1, max_length=200, alias="X-User-Id"),
    x_permissions: str = Header(default="", alias="X-Permissions"),
    x_request_id: str | None = Header(default=None, alias="X-Request-Id"),
    x_idempotency_key: str | None = Header(
        default=None,
        min_length=8,
        max_length=200,
        alias="X-Idempotency-Key",
    ),
    x_trace_id: str = Header(default="", alias="X-Trace-Id"),
    x_tool_execution_id: str = Header(default="", alias="X-Tool-Execution-Id"),
    x_run_id: str = Header(default="", alias="X-Run-Id"),
    x_session_id: str = Header(default="", alias="X-Session-Id"),
    x_agent_id: str = Header(default="", alias="X-Agent-Id"),
    x_agent_version: str = Header(default="", alias="X-Agent-Version"),
    x_snapshot_id: str = Header(default="", alias="X-Snapshot-Id"),
    # FastAPI resolves this declarative dependency at request time; it is not
    # an eager application-side function call.
    x_deadline_at: datetime | None = Header(  # noqa: B008
        default=None, alias="X-Deadline-At"
    ),
    x_attempt_budget_remaining: int | None = Header(
        default=None, ge=0, alias="X-Attempt-Budget-Remaining"
    ),
) -> InvocationContext:
    # When OIDC is enabled, middleware has already verified the JWT and
    # reconstructed these identity headers.  The route intentionally remains
    # header-shaped so local/test callers and existing adapters stay compatible.
    """处理 _context 对应的当前组件内部业务步骤。


    Internal helper for module; preserve its caller-facing invariant.
    """
    return InvocationContext(
        tenant_id=x_tenant_id,
        user_id=x_user_id,
        permissions=frozenset(item.strip() for item in x_permissions.split(",") if item.strip()),
        request_id=x_request_id or f"tool-request-{uuid4().hex}",
        idempotency_key=x_idempotency_key,
        tool_execution_id=x_tool_execution_id,
        trace_id=x_trace_id,
        run_id=x_run_id,
        session_id=x_session_id,
        agent_id=x_agent_id,
        agent_version=x_agent_version,
        snapshot_id=x_snapshot_id,
        deadline_at=x_deadline_at,
        attempt_budget_remaining=x_attempt_budget_remaining,
    )


@router.get("/tools", response_model=list[ToolManifest], tags=["tools"])
def list_tools(
    request: Request,
    context: Annotated[InvocationContext, Depends(_context)],
) -> list[ToolManifest]:
    """读取或查询 list_tools 对应的受控业务步骤。


    List only values visible within the caller's tenant and lifecycle scope.
    """
    return request.app.state.container.registry.manifests(
        context.tenant_id,
        context.permissions,
    )


@router.post(
    "/tools/{tool_name}/invoke",
    response_model=InvocationResponse,
    status_code=status.HTTP_200_OK,
    tags=["tools"],
)
async def invoke_tool(
    tool_name: str,
    payload: InvocationRequest,
    request: Request,
    context: Annotated[InvocationContext, Depends(_context)],
) -> InvocationResponse:
    """处理 invoke_tool 对应的当前组件内部业务步骤。


    Perform invoke tool within the module ownership boundary.
    """
    response = await request.app.state.container.execution.invoke(tool_name, payload, context)
    await request.app.state.container.governance.flush()
    if response.status == InvocationStatus.PENDING_APPROVAL:
        request.scope["tool_gateway_status_code"] = status.HTTP_202_ACCEPTED
    return response


@router.get(
    "/tools/{tool_name}/executions/current",
    response_model=ToolExecutionState,
    tags=["tools"],
)
def get_current_execution(
    tool_name: str,
    request: Request,
    context: Annotated[InvocationContext, Depends(_context)],
) -> ToolExecutionState:
    """按调用方幂等键查询工具执行账本, 供 Runtime 从崩溃窗口安全恢复。

    不接受 URL 参数形式的幂等键, 避免该敏感关联键进入代理日志; 缺失键返回校验错误,
    不能用 request_id 或工具参数替代查询条件。
    """
    if not context.idempotency_key:
        from fastapi import HTTPException

        raise HTTPException(status_code=422, detail="X-Idempotency-Key is required")
    return request.app.state.container.repository.idempotency_state(
        context.tenant_id, tool_name, context.idempotency_key
    )


@router.get(
    "/approvals/{approval_id}",
    response_model=ApprovalRecord,
    tags=["approvals"],
)
def get_approval(
    approval_id: str,
    request: Request,
    context: Annotated[InvocationContext, Depends(_context)],
) -> ApprovalRecord:
    """读取或查询 get_approval 对应的受控业务步骤。


    Return the requested value through the established ownership boundary.
    """
    record = request.app.state.container.repository.get_approval(approval_id)
    if record is None or record.tenant_id != context.tenant_id or record.user_id != context.user_id:
        from app.domain.errors import ApprovalError

        raise ApprovalError("approval does not exist")
    return record


@router.post(
    "/approvals/{approval_id}/approve",
    response_model=ApprovalRecord,
    tags=["approvals"],
)
def approve(
    approval_id: str,
    payload: ApprovalDecision,
    request: Request,
    context: Annotated[InvocationContext, Depends(_context)],
) -> ApprovalRecord:
    """处理 approve 对应的当前组件内部业务步骤。


    Perform approve within the module ownership boundary.
    """
    request.app.state.require_admin(request)
    return request.app.state.container.repository.decide_approval(
        approval_id,
        context.tenant_id,
        approved=True,
        decided_by=context.user_id,
        reason=payload.reason,
    )


@router.post(
    "/approvals/{approval_id}/reject",
    response_model=ApprovalRecord,
    tags=["approvals"],
)
def reject(
    approval_id: str,
    payload: ApprovalDecision,
    request: Request,
    context: Annotated[InvocationContext, Depends(_context)],
) -> ApprovalRecord:
    """处理 reject 对应的当前组件内部业务步骤。


    Perform reject within the module ownership boundary.
    """
    request.app.state.require_admin(request)
    return request.app.state.container.repository.decide_approval(
        approval_id,
        context.tenant_id,
        approved=False,
        decided_by=context.user_id,
        reason=payload.reason,
    )


@router.get("/audit", response_model=AuditPage, tags=["audit"])
def audit(
    request: Request,
    context: Annotated[InvocationContext, Depends(_context)],
    tool_name: str | None = Query(default=None, max_length=150),
    limit: int = Query(default=100, ge=1, le=1_000),
) -> AuditPage:
    """处理 audit 对应的当前组件内部业务步骤。


    Perform audit within the module ownership boundary.
    """
    items = request.app.state.container.repository.list_audit(
        context.tenant_id,
        tool_name=tool_name,
        limit=limit,
    )
    return AuditPage(items=items, count=len(items))
