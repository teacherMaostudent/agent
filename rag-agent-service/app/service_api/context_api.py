import difflib
import hashlib
from io import BytesIO
from urllib.parse import urlparse

from fastapi import APIRouter, Header, HTTPException, Request
from platform_sdk.contracts.artifacts import (
    TaskArtifact,
    TaskArtifactComparison,
    TaskArtifactCreate,
    TaskArtifactPreview,
    TaskArtifactTextCreate,
)

from app.contracts.context import ContextAssembleRequest, ContextPackage, ConversationMessage

router = APIRouter(prefix="/context", tags=["agent-context"])

_PREVIEW_MEDIA_TYPES = {
    "application/json",
    "application/xml",
    "application/yaml",
    "application/x-yaml",
    "text/csv",
    "text/markdown",
    "text/plain",
}


def _artifact_storage_key(container, artifact: TaskArtifact) -> str:
    """Validate that Context owns the object's bucket and configured prefix."""
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
        raise HTTPException(status_code=409, detail="artifact is not deliverable by this storage domain")
    return key


def _preview(container, artifact: TaskArtifact, max_chars: int) -> TaskArtifactPreview:
    """Read and decode only an allow-listed, bounded textual prefix from object storage."""
    media_type = artifact.media_type.split(";", 1)[0].strip().lower()
    if not (media_type.startswith("text/") or media_type in _PREVIEW_MEDIA_TYPES):
        raise HTTPException(status_code=415, detail="artifact media type is not previewable")
    key = _artifact_storage_key(container, artifact)
    bounded_chars = min(max(max_chars, 256), 200_000)
    try:
        payload, byte_truncated = container.artifact_delivery.read_bounded(
            key, max_bytes=min(bounded_chars * 4, 800_000)
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail="artifact storage key is invalid") from exc
    decoded = payload.decode("utf-8", errors="replace")
    char_truncated = len(decoded) > bounded_chars
    content = decoded[:bounded_chars]
    truncated = byte_truncated or char_truncated
    return TaskArtifactPreview(
        artifact_id=artifact.artifact_id,
        logical_name=artifact.logical_name or artifact.artifact_type,
        version=artifact.version,
        media_type=artifact.media_type,
        content=content,
        truncated=truncated,
        content_sha256=artifact.content_sha256,
        sha256_verified=(hashlib.sha256(payload).hexdigest() == artifact.content_sha256)
        if not truncated
        else None,
    )


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
    key = _artifact_storage_key(container, artifact)
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


@router.get(
    "/tasks/{root_task_id}/artifacts/{artifact_id}/preview",
    response_model=TaskArtifactPreview,
)
def artifact_preview(
    root_task_id: str,
    artifact_id: str,
    request: Request,
    max_chars: int = 50_000,
    x_tenant_id: str = Header(default="default", alias="X-Tenant-Id"),
) -> TaskArtifactPreview:
    """Return a bounded text preview after the caller's Runtime-level resource check."""
    container = request.app.state.container
    artifact = container.artifacts.get(x_tenant_id, root_task_id, artifact_id)
    if artifact is None:
        raise HTTPException(status_code=404, detail="task artifact not found")
    return _preview(container, artifact, max_chars)


@router.get(
    "/tasks/{root_task_id}/artifacts/{artifact_id}/compare/{base_artifact_id}",
    response_model=TaskArtifactComparison,
)
def compare_artifacts(
    root_task_id: str,
    artifact_id: str,
    base_artifact_id: str,
    request: Request,
    max_chars: int = 80_000,
    x_tenant_id: str = Header(default="default", alias="X-Tenant-Id"),
) -> TaskArtifactComparison:
    """Compare two textual versions from one logical series using a bounded unified diff."""
    container = request.app.state.container
    target = container.artifacts.get(x_tenant_id, root_task_id, artifact_id)
    base = container.artifacts.get(x_tenant_id, root_task_id, base_artifact_id)
    if target is None or base is None:
        raise HTTPException(status_code=404, detail="task artifact not found")
    target_name = target.logical_name or target.artifact_type
    base_name = base.logical_name or base.artifact_type
    if target_name != base_name:
        raise HTTPException(status_code=409, detail="artifacts do not belong to the same series")
    bounded = min(max(max_chars, 1_000), 200_000)
    target_preview = _preview(container, target, bounded)
    base_preview = _preview(container, base, bounded)
    diff = "".join(
        difflib.unified_diff(
            base_preview.content.splitlines(keepends=True),
            target_preview.content.splitlines(keepends=True),
            fromfile=f"{base_name}@v{base.version}",
            tofile=f"{target_name}@v{target.version}",
        )
    )
    diff_truncated = len(diff) > bounded
    return TaskArtifactComparison(
        base_artifact_id=base.artifact_id,
        target_artifact_id=target.artifact_id,
        logical_name=target_name,
        base_version=base.version,
        target_version=target.version,
        diff=diff[:bounded],
        truncated=base_preview.truncated or target_preview.truncated or diff_truncated,
    )


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
            logical_name=payload.logical_name,
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
