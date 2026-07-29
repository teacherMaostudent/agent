from fastapi import APIRouter, Request

from app.contracts.rag import RagSearchRequest, RagSearchResponse

router = APIRouter(prefix="/query", tags=["rag-query"])


@router.post("/search", response_model=RagSearchResponse)
def search(payload: RagSearchRequest, request: Request) -> RagSearchResponse:
    return request.app.state.container.query_service.search(payload)
