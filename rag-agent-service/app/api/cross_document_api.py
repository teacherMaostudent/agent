"""跨文档审查接口(组件⑥):传多份已上传文件 → 矛盾/职责冲突分析。

见 [[gmp-cross-document-plan]]。每份文件先确保解析出文本(复用 DocumentParser),
再交 CrossDocumentReviewer 做两阶段编排。主题来自 cross-document-topics.json。
"""
import logging

from fastapi import APIRouter, HTTPException

from app.domain.models import CrossDocReport
from app.domain.schemas import AnnotateFindingRequest, CrossDocumentReviewRequest
from app.knowledge.config_loader import load_numeric_topics, load_responsibility_topics
from app.services import container

log = logging.getLogger(__name__)

router = APIRouter(prefix="/reviews", tags=["cross-document"])


@router.post("/cross-document")
def cross_document_review(request: CrossDocumentReviewRequest) -> CrossDocReport:
    if len(request.document_ids) < 2:
        raise HTTPException(status_code=400, detail="跨文档审查至少需要 2 份文件")

    files: list[tuple[str, str, str]] = []
    for doc_id in request.document_ids:
        document = container.repository.get_document(doc_id)
        if document is None:
            raise HTTPException(status_code=404, detail=f"文件不存在: {doc_id}")
        if not document.text:
            text, metadata = container.parser.parse(document.file_path)
            document.text = text
            document.status = "PARSED"
            document.metadata.update(metadata)
            container.repository.save_document(document)
        if document.text.strip():
            files.append((document.document_id, document.filename, document.text))

    if len(files) < 2:
        raise HTTPException(status_code=400, detail="有效文本不足 2 份，无法做跨文档对比")

    report = container.cross_reviewer.review(
        files=files,
        numeric_topics=load_numeric_topics(),
        responsibility_topics=load_responsibility_topics(),
    )
    # 冻结成快照(带 local_id/时间戳),供后续人工标注。见 [[gmp-cross-document-plan]]。
    return container.snapshot_store.save(report)


@router.get("/cross-document/snapshots")
def list_snapshots() -> dict:
    """列出历史快照 id(时间倒序),供前端选择查看。"""
    return {"snapshot_ids": container.snapshot_store.list_ids()}


@router.get("/cross-document/{snapshot_id}")
def get_snapshot(snapshot_id: str) -> CrossDocReport:
    report = container.snapshot_store.get(snapshot_id)
    if report is None:
        raise HTTPException(status_code=404, detail="快照不存在")
    return report


@router.patch("/cross-document/{snapshot_id}/findings/{local_id}")
def annotate_finding(snapshot_id: str, local_id: str, request: AnnotateFindingRequest) -> CrossDocReport:
    """更新某条发现的人工标注(确认/否决/备注)。只改这份快照,不回流。"""
    report = container.snapshot_store.annotate(
        snapshot_id, local_id, verdict=request.verdict, note=request.note
    )
    if report is None:
        raise HTTPException(status_code=404, detail="快照或该条发现不存在")
    return report
