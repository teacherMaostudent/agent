from fastapi import APIRouter, File, HTTPException, UploadFile

from app.domain.models import Document
from app.domain.schemas import ParseResponse
from app.ingestion import ocr_task
from app.services import container

router = APIRouter(prefix="/documents", tags=["documents"])


@router.post("/upload")
def upload_document(file: UploadFile = File(...)) -> Document:
    path, sha256 = container.storage.save_upload(file.filename or "upload.bin", file.file)
    object_key = container.storage.object_key_for(path)
    document = Document(
        filename=file.filename or path.name,
        content_type=file.content_type,
        file_path=path,
        sha256=sha256,
        metadata={**({"object_key": object_key} if object_key else {})},
    )
    return container.repository.save_document(document)


@router.post("/{document_id}/parse")
def parse_document(document_id: str) -> ParseResponse:
    document = container.repository.get_document(document_id)
    if document is None:
        raise HTTPException(status_code=404, detail="document not found")
    text, metadata = container.parser.parse(document.file_path)
    document.text = text
    document.metadata.update(metadata)
    document.status = "PARSED"
    container.repository.save_document(document)
    return ParseResponse(document_id=document.document_id, status=document.status, text_length=len(text), metadata=document.metadata)


@router.post("/{document_id}/ocr")
def start_ocr(document_id: str) -> dict:
    """触发扫描版 PDF 的后台 OCR(数分钟,不卡请求)。已在跑则不重复触发。

    纯本地 RapidOCR 识别,不外传(保密件)。用 GET status 轮询进度。
    """
    document = container.repository.get_document(document_id)
    if document is None:
        raise HTTPException(status_code=404, detail="document not found")
    started = ocr_task.start_ocr(document_id, container)
    return {
        "document_id": document_id,
        "started": started,
        "status": "OCR_RUNNING" if started else document.status,
        "message": "已开始后台识别" if started else "该文档已在识别中",
    }


@router.get("/{document_id}/status")
def document_status(document_id: str) -> dict:
    """查文档解析/OCR 状态与进度,供前端轮询显示"已识别 N/总页"。"""
    document = container.repository.get_document(document_id)
    if document is None:
        raise HTTPException(status_code=404, detail="document not found")
    return {
        "document_id": document_id,
        "status": document.status,
        "ocr_done": document.metadata.get("ocr_done"),
        "ocr_total": document.metadata.get("ocr_total"),
        "text_length": len(document.text),
        "source": document.metadata.get("source"),
        "error": document.metadata.get("ocr_error"),
    }

