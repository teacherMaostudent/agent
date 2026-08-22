import hashlib
from pathlib import Path
from urllib.parse import urlparse

from fastapi import APIRouter, File, Header, HTTPException, Request, UploadFile
from platform_sdk.contracts.ingestion import ApprovedArtifactIngestion, ArtifactIngestionReceipt

from app.contracts.ingestion import IngestionJob, JobCreateRequest
from app.domain.models import Document

router = APIRouter(prefix="/ingestion", tags=["knowledge-ingestion"])


@router.post("/artifacts", response_model=ArtifactIngestionReceipt, status_code=202)
def ingest_approved_artifact(
    payload: ApprovedArtifactIngestion,
    request: Request,
    x_tenant_id: str = Header(default="default", alias="X-Tenant-Id"),
    x_user_id: str = Header(default="anonymous", alias="X-User-Id"),
) -> ArtifactIngestionReceipt:
    """Promote one approved Context Artifact into the durable ingestion queue.

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
    if payload.job_type in {"PARSE", "OCR"} and not payload.document_id:
        raise HTTPException(status_code=400, detail=f"{payload.job_type} requires document_id")
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
