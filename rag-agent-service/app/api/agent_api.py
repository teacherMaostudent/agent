from uuid import uuid4

from fastapi import APIRouter, Header, HTTPException

from app.domain.schemas import AgentRunRequest
from app.services import container

router = APIRouter(prefix="/agent", tags=["agent"])


@router.post("/run")
def run_agent(
    request: AgentRunRequest,
    x_tenant_id: str = Header(default="default", alias="X-Tenant-Id"),
    x_user_id: str = Header(default="anonymous", alias="X-User-Id"),
    x_permissions: str = Header(default="rag:read", alias="X-Permissions"),
    x_request_id: str | None = Header(default=None, alias="X-Request-Id"),
) -> dict:
    """Run the bounded Agent Graph; identity and permissions only come from trusted headers."""
    if not container.settings.agent_enabled:
        raise HTTPException(status_code=503, detail="agent graph is disabled")
    permissions = {item.strip() for item in x_permissions.split(",") if item.strip()}
    if "rag:read" not in permissions:
        raise HTTPException(status_code=403, detail="rag:read permission is required")
    request_id = x_request_id or f"rag-{uuid4().hex}"
    thread_id = f"{x_tenant_id}:{x_user_id}:{request.session_id or request_id}"
    result = container.agent_graph.run(
        {
            "task": request.task,
            "document_id": request.document_id,
            "content": request.content,
            "metadata": request.metadata,
            "tenant_id": x_tenant_id,
            "user_id": x_user_id,
            "permissions": sorted(permissions),
            "request_id": request_id,
            "session_id": request.session_id or request_id,
            "step_count": 0,
            "max_steps": request.max_steps or container.settings.agent_max_steps,
            "observations": [],
            "evidence": [],
        },
        thread_id,
    )
    return result.model_dump(mode="json")
