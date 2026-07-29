from fastapi import APIRouter, HTTPException

from app.domain.models import Document, ReviewResult, new_id
from app.domain.schemas import GmpReviewRequest
from app.services import container

router = APIRouter(prefix="/reviews", tags=["reviews"])


@router.post("/gmp")
def create_gmp_review(request: GmpReviewRequest) -> ReviewResult:
    document_id, text = _resolve_review_text(request)
    doc_type = request.document_type if request.document_type != "gmp_document" else None
    review = container.reviewer.review(document_id=document_id, text=text, document_type=doc_type)
    review.report_path = container.storage.save_report(review.review_id, review.report_markdown)
    container.repository.save_review(review)
    return review


@router.get("/{review_id}")
def get_review(review_id: str) -> ReviewResult:
    review = container.repository.get_review(review_id)
    if review is None:
        raise HTTPException(status_code=404, detail="review not found")
    return review


@router.post("/{review_id}/rerun")
def rerun_review(review_id: str) -> ReviewResult:
    review = container.repository.get_review(review_id)
    if review is None:
        raise HTTPException(status_code=404, detail="review not found")
    document = container.repository.get_document(review.document_id)
    if document is None:
        raise HTTPException(status_code=404, detail="document not found")
    return create_gmp_review(GmpReviewRequest(document_id=document.document_id))


def _resolve_review_text(request: GmpReviewRequest) -> tuple[str, str]:
    if request.document_id:
        document = container.repository.get_document(request.document_id)
        if document is None:
            raise HTTPException(status_code=404, detail="document not found")
        if not document.text:
            text, metadata = container.parser.parse(document.file_path)
            document.text = text
            document.status = "PARSED"
            document.metadata.update(metadata)
            container.repository.save_document(document)
        return document.document_id, document.text
    if request.content:
        document = Document(
            document_id=new_id("inline_doc"),
            filename="inline-content.md",
            content_type="text/markdown",
            file_path=container.settings.upload_dir / "inline-content.md",
            sha256=new_id("sha"),
            status="PARSED",
            text=request.content,
            metadata=request.metadata,
        )
        container.repository.save_document(document)
        return document.document_id, document.text
    raise HTTPException(status_code=400, detail="document_id or content is required")

