from fastapi import APIRouter, Header, HTTPException, Request

from app.contracts.context import ContextAssembleRequest, ContextPackage, ConversationMessage

router = APIRouter(prefix="/context", tags=["agent-context"])


@router.post("/assemble", response_model=ContextPackage)
def assemble(payload: ContextAssembleRequest, request: Request) -> ContextPackage:
    return request.app.state.container.context_service.assemble(payload)


@router.post("/sessions/{session_id}/messages", status_code=204)
def append_message(
    session_id: str,
    payload: ConversationMessage,
    request: Request,
    x_tenant_id: str = Header(default="default", alias="X-Tenant-Id"),
    x_user_id: str = Header(default="anonymous", alias="X-User-Id"),
) -> None:
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
    return request.app.state.container.context_service.messages(
        session_id, x_tenant_id, x_user_id
    )


@router.delete("/sessions/{session_id}", status_code=204)
def delete_session(
    session_id: str,
    request: Request,
    x_tenant_id: str = Header(default="default", alias="X-Tenant-Id"),
    x_user_id: str = Header(default="anonymous", alias="X-User-Id"),
) -> None:
    deleted = request.app.state.container.context_service.delete_session(
        session_id, x_tenant_id, x_user_id
    )
    if not deleted:
        raise HTTPException(status_code=404, detail="session not found")
