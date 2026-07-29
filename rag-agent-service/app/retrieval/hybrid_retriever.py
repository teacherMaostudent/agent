from app.domain.models import Chunk, Evidence
from app.retrieval.bm25_retriever import BM25Retriever
from app.retrieval.vector_retriever import HashEmbeddingRetriever


class HybridRetriever:
    def __init__(
        self,
        bm25_weight: float,
        vector_weight: float,
        embedding_dim: int,
        reranker=None,
        candidate_k: int | None = None,
    ) -> None:
        self.bm25 = BM25Retriever()
        self.vector = HashEmbeddingRetriever(embedding_dim)
        self.bm25_weight = bm25_weight
        self.vector_weight = vector_weight
        self.reranker = reranker
        self.candidate_k = candidate_k

    def search(self, query: str, chunks: list[Chunk], top_k: int) -> list[Evidence]:
        candidate_k = max(top_k, self.candidate_k or top_k * 4)
        bm25_hits = self.bm25.search(query, chunks, candidate_k)
        vector_hits = self.vector.search(query, chunks, candidate_k)
        merged: dict[str, Evidence] = {}
        for hit in bm25_hits:
            key = f"{hit.source_id}:{hit.metadata.get('start', 0)}"
            hit.score = self._normalize(hit.score, bm25_hits) * self.bm25_weight
            merged[key] = hit
        for hit in vector_hits:
            key = f"{hit.source_id}:{hit.metadata.get('start', 0)}"
            weighted = self._normalize(hit.score, vector_hits) * self.vector_weight
            if key in merged:
                merged[key].score += weighted
            else:
                hit.score = weighted
                merged[key] = hit
        candidates = sorted(merged.values(), reverse=True, key=lambda item: item.score)
        if self.reranker is not None:
            return self.reranker.rerank(query, candidates, top_k)
        return candidates[:top_k]

    @staticmethod
    def _normalize(score: float, hits: list[Evidence]) -> float:
        max_score = max((hit.score for hit in hits), default=1.0)
        return score / max(max_score, 1e-9)
