from fastapi import APIRouter, Request

from app.contracts.rag import (
    ControlledScanRequest,
    ControlledScanResponse,
    RagCapabilitiesResponse,
    RagIndexVersionResponse,
    RagSearchRequest,
    RagSearchResponse,
)

router = APIRouter(prefix="/query", tags=["rag-query"])


@router.get("/capabilities", response_model=RagCapabilitiesResponse)
def capabilities() -> RagCapabilitiesResponse:
    """Publish the supported RAG contract without exposing internal modules."""
    return RagCapabilitiesResponse()


@router.get("/index-version", response_model=RagIndexVersionResponse)
def index_version(request: Request) -> RagIndexVersionResponse:
    """Expose the active immutable index version for release/runtime validation."""
    query_service = request.app.state.container.query_service
    return RagIndexVersionResponse(
        index_version=query_service.index_version,
        backend=query_service.backend,
    )


@router.post("/search", response_model=RagSearchResponse)
def search(payload: RagSearchRequest, request: Request) -> RagSearchResponse:
    return request.app.state.container.query_service.search(payload)


@router.post("/scan", response_model=ControlledScanResponse)
def scan(payload: ControlledScanRequest, request: Request) -> ControlledScanResponse:
    matches = request.app.state.container.query_service.scan(
        payload.scope, payload.pattern, regex=payload.regex, glob=payload.glob
    )
    return ControlledScanResponse(scope=payload.scope, matches=matches)
