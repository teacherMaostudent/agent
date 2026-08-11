from fastapi import APIRouter, Header, HTTPException, Request

from app.contracts.context import ContextAssembleRequest, ContextPackage, ConversationMessage

router = APIRouter(prefix="/context", tags=["agent-context"])


@router.post("/assemble", response_model=ContextPackage)
def assemble(payload: ContextAssembleRequest, request: Request) -> ContextPackage:
    """组装可直接进入 Runtime Prompt 的上下文包；RAG 不可用时按请求约束降级。"""
    return request.app.state.container.context_service.assemble(payload)


@router.post("/sessions/{session_id}/messages", status_code=204)
def append_message(
    session_id: str,
    payload: ConversationMessage,
    request: Request,
    x_tenant_id: str = Header(default="default", alias="X-Tenant-Id"),
    x_user_id: str = Header(default="anonymous", alias="X-User-Id"),
) -> None:
    """追加一条会话消息；租户和用户应在生产中来自已验证的身份声明而非 Header。"""
    request.app.state.container.context_service.append_message(
        session_id, payload, x_tenant_id, x_user_id
    )


@router.get("/sessions/{session_id}/messages", response_model=list[ConversationMessage])
def list_messages(
    session_id: str,
    request: Request,
    x_tenant_id: str = Header(default="default", alias="X-Tenant-Id"),
    x_user_id: str = Header(default="anonymous", alias="X-User-Id"),
) -> list[ConversationMessage]:
    """获取当前主体的会话历史；服务层使用三元键隔离多租户数据。"""
    return request.app.state.container.context_service.messages(session_id, x_tenant_id, x_user_id)


@router.delete("/sessions/{session_id}", status_code=204)
def delete_session(
    session_id: str,
    request: Request,
    x_tenant_id: str = Header(default="default", alias="X-Tenant-Id"),
    x_user_id: str = Header(default="anonymous", alias="X-User-Id"),
) -> None:
    """删除当前主体会话；不存在时返回 404，便于调用方确认清理结果。"""
    deleted = request.app.state.container.context_service.delete_session(
        session_id, x_tenant_id, x_user_id
    )
    if not deleted:
        raise HTTPException(status_code=404, detail="session not found")
