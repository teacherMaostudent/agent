from fastapi import APIRouter, File, Header, HTTPException, Request, UploadFile

from app.contracts.ingestion import IngestionJob, JobCreateRequest
from app.domain.models import Document

router = APIRouter(prefix="/ingestion", tags=["knowledge-ingestion"])


@router.post("/documents", status_code=202)
def upload_document(
    request: Request,
    file: UploadFile = File(...),
    x_tenant_id: str = Header(default="default", alias="X-Tenant-Id"),
    x_user_id: str = Header(default="anonymous", alias="X-User-Id"),
) -> dict:
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
    document = request.app.state.container.repository.get_document(document_id)
    if document is None or document.metadata.get("tenant_id") != x_tenant_id:
        raise HTTPException(status_code=404, detail="document not found")
    return document
