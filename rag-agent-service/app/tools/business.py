from pydantic import BaseModel, Field

from app.tools.registry import ToolContext, ToolRegistry


class GetDocumentArgs(BaseModel):
    document_id: str = Field(min_length=3, max_length=160)


class ReviewDocumentArgs(BaseModel):
    document_id: str = Field(min_length=3, max_length=160)
    document_type: str | None = Field(default=None, max_length=160)


def build_business_tool_registry(repository, reviewer, default_timeout: float) -> ToolRegistry:
    registry = ToolRegistry(default_timeout=default_timeout)

    def get_document(args: GetDocumentArgs, context: ToolContext) -> dict:
        document = repository.get_document(args.document_id)
        if document is None:
            raise ValueError("document not found")
        return {
            "document_id": document.document_id,
            "filename": document.filename,
            "status": document.status,
            "text": document.text[:12000],
            "truncated": len(document.text) > 12000,
        }

    def review_document(args: ReviewDocumentArgs, context: ToolContext) -> dict:
        document = repository.get_document(args.document_id)
        if document is None:
            raise ValueError("document not found")
        if not document.text:
            raise ValueError("document has not been parsed")
        review = reviewer.review(args.document_id, document.text, args.document_type)
        return {
            "review_id": review.review_id,
            "overall_risk": review.overall_risk,
            "summary": review.summary,
            "coverage": review.coverage.model_dump(mode="json"),
        }

    registry.register(
        "get_document",
        "Read one already uploaded enterprise document by id.",
        GetDocumentArgs,
        get_document,
        {"document:read"},
    )
    registry.register(
        "run_gmp_review",
        "Run the deterministic GMP review chain for one parsed document.",
        ReviewDocumentArgs,
        review_document,
        {"document:read", "review:execute"},
        timeout_seconds=max(default_timeout, 60.0),
    )
    return registry
