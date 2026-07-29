from app.bootstrap.repository import build_repository
from app.core.config import get_settings
from app.rag.query_service import RagQueryService
from app.rerank import build_reranker
from app.retrieval.hybrid_retriever import HybridRetriever


class RagQueryContainer:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.repository = build_repository(self.settings)
        self.reranker = build_reranker(self.settings)
        self.retriever = HybridRetriever(
            bm25_weight=self.settings.bm25_weight,
            vector_weight=self.settings.vector_weight,
            embedding_dim=self.settings.local_embedding_dim,
            reranker=self.reranker,
            candidate_k=self.settings.retrieval_candidate_k,
        )
        self.query_service = RagQueryService(self.repository, self.retriever)

    def close(self) -> None:
        close = getattr(self.repository, "close", None)
        if close is not None:
            close()
