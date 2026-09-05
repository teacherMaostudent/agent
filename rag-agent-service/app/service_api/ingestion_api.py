import hashlib
import io
from pathlib import Path
from urllib.parse import urlparse

from fastapi import APIRouter, File, Header, HTTPException, Request, UploadFile
from platform_sdk.contracts.ingestion import (
    ApprovedArtifactIngestion,
    ApprovedWikiPageIngestion,
    ArtifactIngestionReceipt,
    WikiPageIngestionReceipt,
)
from platform_sdk.contracts.rag import IndexBuildManifest

from app.contracts.ingestion import IngestionJob, JobCreateRequest
from app.domain.models import Document

router = APIRouter(prefix="/ingestion", tags=["knowledge-ingestion"])


@router.post("/sources/{source_id}/status")
def update_source_status(
    source_id: str,
    payload: dict,
    request: Request,
    x_tenant_id: str = Header(default="default", alias="X-Tenant-Id"),
    x_permissions: str = Header(default="", alias="X-Permissions"),
) -> dict:
    """Apply a durable upstream source lifecycle event and deactivate its index projection.

    This endpoint is for a source-system relay, not arbitrary document edits.
    It deliberately requires a dedicated permission and writes repository
    truth first.  A retry after a projection failure is safe because setting
    the same status is idempotent.
    """

    permissions = {item.strip() for item in x_permissions.split(",") if item.strip()}
    if "rag:source:revoke" not in permissions:
        raise HTTPException(status_code=403, detail="rag:source:revoke permission is required")
    status = str(payload.get("status", "")).lower()
    if status not in {"active", "revoked", "quarantined", "untrusted"}:
        raise HTTPException(status_code=422, detail="unsupported source status")
    container = request.app.state.container
    documents = container.repository.set_source_status(
        x_tenant_id,
        source_id,
        status,
        reason=str(payload.get("reason", ""))[:2000],
    )
    projected = container.search_projection.update_source_status(x_tenant_id, source_id, status)
    return {
        "tenant_id": x_tenant_id,
        "source_id": source_id,
        "status": status,
        "authoritative_documents_updated": len(documents),
        "indexed_chunks_updated": projected,
    }


@router.post("/wiki-pages", response_model=WikiPageIngestionReceipt, status_code=202)
def ingest_approved_wiki_page(
    payload: ApprovedWikiPageIngestion,
    request: Request,
    x_tenant_id: str = Header(default="default", alias="X-Tenant-Id"),
    x_user_id: str = Header(default="anonymous", alias="X-User-Id"),
) -> WikiPageIngestionReceipt:
    """Persist one approved Wiki version and enqueue its idempotent parse/index job.

    The page identity includes tenant, page and immutable version. Relay retries therefore
    return the same document/job pair instead of creating duplicate searchable knowledge.
    The supplied digest is verified before persistence so a compromised relay cannot attach
    different content to an already approved Wiki version.
    """
    container = request.app.state.container
    encoded = payload.markdown.encode("utf-8")
    if hashlib.sha256(encoded).hexdigest() != payload.content_sha256:
        raise HTTPException(status_code=422, detail="Wiki content digest mismatch")
    stable = hashlib.sha256(
        f"{x_tenant_id}:{payload.page_id}:{payload.version}".encode()
    ).hexdigest()[:20]
    document_id = f"doc_wiki_{stable}"
    job_id = f"job_wiki_{stable}"
    existing_job = container.job_store.get(job_id, x_tenant_id)
    existing_document = container.repository.get_document(document_id)
    if existing_document is not None and (
        existing_document.sha256 != payload.content_sha256
        or existing_document.metadata.get("page_id") != payload.page_id
        or existing_document.metadata.get("version") != payload.version
    ):
        raise HTTPException(status_code=409, detail="Wiki document identity collision")
    if existing_job is None:
        safe_name = Path(payload.title).name[:100] or payload.page_id
        path, stored_sha256 = container.storage.save_upload(
            f"{safe_name}-v{payload.version}.md", io.BytesIO(encoded)
        )
        if stored_sha256 != payload.content_sha256:
            raise HTTPException(status_code=500, detail="stored Wiki content digest mismatch")
        object_key = container.storage.object_key_for(path)
        document = existing_document or container.repository.save_document(
            Document(
                document_id=document_id,
                filename=f"{safe_name}-v{payload.version}.md",
                content_type="text/markdown; charset=utf-8",
                file_path=path,
                sha256=payload.content_sha256,
                metadata={
                    "tenant_id": x_tenant_id,
                    "uploaded_by": x_user_id,
                    "source": "human-approved-wiki",
                    "page_id": payload.page_id,
                    "candidate_id": payload.candidate_id,
                    "version": payload.version,
                    "approved_by": payload.approved_by,
                    "source_ids": payload.source_ids,
                    "knowledge_status": "active",
                    "valid_until": payload.valid_until.isoformat() if payload.valid_until else None,
                    "supersedes_page_ids": payload.supersedes_page_ids,
                    **({"object_key": object_key} if object_key else {}),
                },
            )
        )
        existing_job = container.job_store.create(
            IngestionJob(
                job_id=job_id,
                job_type="PARSE",
                document_id=document.document_id,
                tenant_id=x_tenant_id,
                requested_by=x_user_id,
            )
        )
    return WikiPageIngestionReceipt(
        page_id=payload.page_id,
        version=payload.version,
        document_id=document_id,
        job_id=job_id,
        status=existing_job.status.value,
    )


@router.post("/artifacts", response_model=ArtifactIngestionReceipt, status_code=202)
def ingest_approved_artifact(
    payload: ApprovedArtifactIngestion,
    request: Request,
    x_tenant_id: str = Header(default="default", alias="X-Tenant-Id"),
    x_user_id: str = Header(default="anonymous", alias="X-User-Id"),
) -> ArtifactIngestionReceipt:
    """将一个已审批的 Context Artifact 晋升到持久摄取队列。

    The endpoint accepts only an object in this service's configured bucket/prefix.
    Deterministic document/job IDs make Runtime relay retries safe and preserve the
    human approval identity in knowledge provenance.
    """
    container = request.app.state.container
    storage = container.storage
    objects = getattr(storage, "objects", None)
    if objects is None:
        raise HTTPException(status_code=409, detail="artifact ingestion requires S3 storage")
    reference = urlparse(payload.content_ref)
    object_key = reference.path.lstrip("/")
    allowed_prefix = str(getattr(objects, "prefix", "")).strip("/")
    if (
        reference.scheme != "s3"
        or reference.netloc != str(objects.bucket)
        or not object_key
        or (allowed_prefix and not object_key.startswith(f"{allowed_prefix}/"))
    ):
        raise HTTPException(status_code=422, detail="artifact object is outside ingestion storage")
    stable = hashlib.sha256(f"{x_tenant_id}:{payload.artifact_id}".encode()).hexdigest()[:20]
    document_id = f"doc_artifact_{stable}"
    job_id = f"job_artifact_{stable}"
    existing_job = container.job_store.get(job_id, x_tenant_id)
    if existing_job is None:
        existing_document = container.repository.get_document(document_id)
        if existing_document is not None and (
            existing_document.metadata.get("tenant_id") != x_tenant_id
            or existing_document.metadata.get("artifact_id") != payload.artifact_id
        ):
            raise HTTPException(status_code=409, detail="artifact document identity collision")
        safe_name = Path(payload.logical_name or payload.artifact_id).name[:120] or "artifact"
        document = existing_document or container.repository.save_document(
            Document(
                document_id=document_id,
                filename=f"{safe_name}.json",
                content_type=payload.media_type,
                file_path=container.settings.upload_dir / f"{payload.content_sha256[:16]}_{safe_name}",
                sha256=payload.content_sha256,
                metadata={
                    "tenant_id": x_tenant_id,
                    "uploaded_by": x_user_id,
                    "source": "desktop-approved-artifact",
                    "object_key": object_key,
                    "artifact_id": payload.artifact_id,
                    "root_task_id": payload.root_task_id,
                    "approval_id": payload.approval_id,
                    "approved_by": payload.approved_by,
                },
            )
        )
        existing_job = container.job_store.create(
            IngestionJob(
                job_id=job_id,
                job_type="PARSE",
                document_id=document.document_id,
                tenant_id=x_tenant_id,
                requested_by=x_user_id,
            )
        )
    return ArtifactIngestionReceipt(
        artifact_id=payload.artifact_id,
        document_id=document_id,
        job_id=job_id,
        status=existing_job.status.value,
    )


@router.post("/documents", status_code=202)
def upload_document(
    request: Request,
    file: UploadFile = File(...),
    x_tenant_id: str = Header(default="default", alias="X-Tenant-Id"),
    x_user_id: str = Header(default="anonymous", alias="X-User-Id"),
) -> dict:
    """保存上传原件和租户归属后创建解析任务；API 不在请求线程同步解析大文件。"""
    container = request.app.state.container
    path, sha256 = container.storage.save_upload(file.filename or "upload.bin", file.file)
    object_key = container.storage.object_key_for(path)
    document = container.repository.save_document(
        Document(
            filename=file.filename or path.name,
            content_type=file.content_type,
            file_path=path,
            sha256=sha256,
            metadata={
                "tenant_id": x_tenant_id,
                "uploaded_by": x_user_id,
                **({"object_key": object_key} if object_key else {}),
            },
        )
    )
    job = container.job_store.create(
        IngestionJob(
            job_type="PARSE",
            document_id=document.document_id,
            tenant_id=x_tenant_id,
            requested_by=x_user_id,
        )
    )
    return {"document": document.model_dump(mode="json"), "job": job.model_dump(mode="json")}


@router.post("/jobs", response_model=IngestionJob, status_code=202)
def create_job(
    payload: JobCreateRequest,
    request: Request,
    x_tenant_id: str = Header(default="default", alias="X-Tenant-Id"),
    x_user_id: str = Header(default="anonymous", alias="X-User-Id"),
) -> IngestionJob:
    """创建显式摄取任务；需要文档的任务在入队前校验，避免 Worker 无效重试。"""
    if payload.job_type in {"PARSE", "OCR", "REINDEX"} and not payload.document_id:
        raise HTTPException(status_code=400, detail=f"{payload.job_type} requires document_id")
    if payload.job_type == "REINDEX_KNOWLEDGE_BASE" and payload.document_id:
        raise HTTPException(
            status_code=422,
            detail="REINDEX_KNOWLEDGE_BASE is scoped by payload.knowledge_base, not document_id",
        )
    job = IngestionJob(
        job_type=payload.job_type,
        document_id=payload.document_id,
        payload=payload.payload,
        tenant_id=x_tenant_id,
        requested_by=x_user_id,
    )
    return request.app.state.container.job_store.create(job)


@router.get("/jobs/{job_id}", response_model=IngestionJob)
def get_job(
    job_id: str,
    request: Request,
    x_tenant_id: str = Header(default="default", alias="X-Tenant-Id"),
) -> IngestionJob:
    """按 job_id 与 tenant_id 查询任务，未授权和不存在均返回 404 防止枚举。"""
    job = request.app.state.container.job_store.get(job_id, x_tenant_id)
    if job is None:
        raise HTTPException(status_code=404, detail="ingestion job not found")
    return job


@router.get("/documents/{document_id}", response_model=Document)
def get_document(
    document_id: str,
    request: Request,
    x_tenant_id: str = Header(default="default", alias="X-Tenant-Id"),
) -> Document:
    """读取当前租户拥有的文档；跨租户和不存在统一隐藏为 404。"""
    document = request.app.state.container.repository.get_document(document_id)
    if document is None or document.metadata.get("tenant_id") != x_tenant_id:
        raise HTTPException(status_code=404, detail="document not found")
    return document


@router.get("/index-manifests/{manifest_id}", response_model=IndexBuildManifest)
def get_index_manifest(
    manifest_id: str,
    request: Request,
    x_tenant_id: str = Header(default="default", alias="X-Tenant-Id"),
) -> IndexBuildManifest:
    """Expose immutable build provenance only to the owning tenant.

    The endpoint is intentionally read-only: publish decisions remain in
    Control Plane and an ingestion caller cannot mark a partial build READY.
    """

    manifest = request.app.state.container.repository.get_index_manifest(manifest_id)
    if manifest is None or manifest.tenant_id != x_tenant_id:
        raise HTTPException(status_code=404, detail="index build manifest not found")
    return manifest
