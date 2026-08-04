from fastapi import APIRouter, Request

from app.contracts.rag import (
    ControlledScanRequest,
    ControlledScanResponse,
    RagSearchRequest,
    RagSearchResponse,
)

router = APIRouter(prefix="/query", tags=["rag-query"])


@router.post("/search", response_model=RagSearchResponse)
def search(payload: RagSearchRequest, request: Request) -> RagSearchResponse:
    return request.app.state.container.query_service.search(payload)


@router.post("/scan", response_model=ControlledScanResponse)
def scan(payload: ControlledScanRequest, request: Request) -> ControlledScanResponse:
    matches = request.app.state.container.query_service.scan(
        payload.scope, payload.pattern, regex=payload.regex, glob=payload.glob
    )
    return ControlledScanResponse(scope=payload.scope, matches=matches)
