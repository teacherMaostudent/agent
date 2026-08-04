from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request

from app.lite_compliance.models import (
    ExternalReviewRequest,
    FeedbackInput,
    InternalReviewRequest,
    LiteDocument,
    RegulationClause,
    ReviewJob,
)

router = APIRouter(prefix="/lite", tags=["lite-compliance"])


@router.post("/documents/bulk")
def register_documents(documents: list[LiteDocument], request: Request) -> dict:
    return request.app.state.container.service.register_documents(documents)


@router.get("/documents", response_model=list[LiteDocument])
def list_documents(request: Request) -> list[LiteDocument]:
    return request.app.state.container.store.documents()


@router.post("/regulation-clauses/bulk")
def register_clauses(clauses: list[RegulationClause], request: Request) -> dict:
    return request.app.state.container.service.register_clauses(clauses)


@router.get("/regulation-clauses", response_model=list[RegulationClause])
def list_clauses(request: Request) -> list[RegulationClause]:
    return request.app.state.container.store.clauses()


@router.post("/reviews/external", response_model=ReviewJob)
def external_review(payload: ExternalReviewRequest, request: Request) -> ReviewJob:
    return request.app.state.container.service.external_review(payload)


@router.post("/reviews/internal", response_model=ReviewJob)
def internal_review(payload: InternalReviewRequest, request: Request) -> ReviewJob:
    return request.app.state.container.service.internal_review(payload)


@router.get("/reviews", response_model=list[ReviewJob])
def list_reviews(request: Request) -> list[ReviewJob]:
    return request.app.state.container.store.jobs()


@router.get("/reviews/{job_id}", response_model=ReviewJob)
def get_review(job_id: str, request: Request) -> ReviewJob:
    job = request.app.state.container.store.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="review not found")
    return job


@router.post("/feedback")
def submit_feedback(payload: FeedbackInput, request: Request) -> dict:
    return request.app.state.container.service.submit_feedback(payload)


@router.get("/history")
def history(
    request: Request,
    event_type: str | None = Query(default=None),
    object_id: str | None = Query(default=None),
) -> dict:
    items = request.app.state.container.store.events(event_type, object_id)
    return {"items": items, "count": len(items)}
