from io import BytesIO
from urllib.parse import urlparse

from fastapi import APIRouter, Header, HTTPException, Request
from platform_sdk.contracts.artifacts import (
    TaskArtifact,
    TaskArtifactCreate,
    TaskArtifactTextCreate,
)

from app.contracts.context import ContextAssembleRequest, ContextPackage, ConversationMessage

router = APIRouter(prefix="/context", tags=["agent-context"])


@router.post("/tasks/{root_task_id}/artifacts", response_model=TaskArtifact, status_code=201)
def create_artifact(
    root_task_id: str,
    payload: TaskArtifactCreate,
    request: Request,
    x_tenant_id: str = Header(default="default", alias="X-Tenant-Id"),
    x_user_id: str = Header(default="anonymous", alias="X-User-Id"),
) -> TaskArtifact:
    """创建 RootTask 中间成果引用；正文不会复制进 Context 数据库。"""
    if payload.root_task_id != root_task_id:
        raise HTTPException(status_code=400, detail="root_task_id does not match request path")
    return request.app.state.container.artifacts.create(x_tenant_id, x_user_id, payload)


@router.get("/tasks/{root_task_id}/artifacts/{artifact_id}", response_model=TaskArtifact)
def get_artifact(
    root_task_id: str,
    artifact_id: str,
    request: Request,
    x_tenant_id: str = Header(default="default", alias="X-Tenant-Id"),
) -> TaskArtifact:
    """按 RootTask 范围读取引用，禁止只凭 Artifact ID 跨任务访问。"""
    artifact = request.app.state.container.artifacts.get(x_tenant_id, root_task_id, artifact_id)
    if artifact is None:
        raise HTTPException(status_code=404, detail="task artifact not found")
    return artifact


@router.get("/tasks/{root_task_id}/artifacts", response_model=list[TaskArtifact])
def list_artifacts(
    root_task_id: str,
    request: Request,
    limit: int = 100,
    x_tenant_id: str = Header(default="default", alias="X-Tenant-Id"),
) -> list[TaskArtifact]:
    """返回任务的不可变工件索引；正文仍只能经受控内容域按引用读取。"""
    return request.app.state.container.artifacts.list(
        x_tenant_id, root_task_id, limit=min(max(limit, 1), 200)
    )


@router.get("/tasks/{root_task_id}/artifacts/{artifact_id}/download-url")
def artifact_download_url(
    root_task_id: str,
    artifact_id: str,
    request: Request,
    x_tenant_id: str = Header(default="default", alias="X-Tenant-Id"),
) -> dict[str, str | int]:
    """为已存在 Artifact 签发短期下载 URL，不允许把任意 s3:// 引用变成签名能力。"""
    container = request.app.state.container
    artifact = container.artifacts.get(x_tenant_id, root_task_id, artifact_id)
    if artifact is None:
        raise HTTPException(status_code=404, detail="task artifact not found")
    storage = container.artifact_delivery
    if storage is None:
        raise HTTPException(status_code=409, detail="artifact delivery is not configured")
    parsed = urlparse(artifact.content_ref)
    required_prefix = storage.prefix.strip("/")
    key = parsed.path.lstrip("/")
    if (
        parsed.scheme != "s3"
        or parsed.netloc != storage.bucket
        or (required_prefix and not key.startswith(f"{required_prefix}/"))
    ):
        # TaskArtifact 可以引用其他业务域内容，但 Context 绝不能为它们签名；应由该
        # 业务域提供自己的授权下载器。
        raise HTTPException(status_code=409, detail="artifact is not deliverable by this storage domain")
    try:
        return {
            "url": storage.presign_download(key),
            "expires_in_seconds": 300,
            # S3-compatible GET signatures authorize byte-range requests without signing each
            # Range header, enabling resumable downloads while keeping the same short expiry.
            "supports_range": True,
        }
    except ValueError as exc:
        raise HTTPException(status_code=409, detail="artifact storage key is invalid") from exc


@router.post("/tasks/{root_task_id}/artifacts/text", response_model=TaskArtifact, status_code=201)
def create_text_artifact(
    root_task_id: str,
    payload: TaskArtifactTextCreate,
    request: Request,
    x_tenant_id: str = Header(default="default", alias="X-Tenant-Id"),
    x_user_id: str = Header(default="anonymous", alias="X-User-Id"),
) -> TaskArtifact:
    """把受信任 Runtime 的小型最终报告写入配置 S3，再登记不可变 Artifact 引用。"""
    storage = request.app.state.container.artifact_delivery
    if storage is None:
        raise HTTPException(status_code=409, detail="artifact delivery is not configured")
    encoded = payload.content.encode("utf-8")
    key, checksum = storage.put_stream(
        f"tasks/{x_tenant_id}/{root_task_id}",
        "final-report.md",
        BytesIO(encoded),
        content_type=payload.media_type,
    )
    return request.app.state.container.artifacts.create(
        x_tenant_id,
        x_user_id,
        TaskArtifactCreate(
            root_task_id=root_task_id,
            artifact_type=payload.artifact_type,
            content_ref=f"s3://{storage.bucket}/{key}",
            content_sha256=checksum,
            media_type=payload.media_type,
        ),
    )


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
