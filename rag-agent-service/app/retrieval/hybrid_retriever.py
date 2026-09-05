from app.domain.models import Chunk, RetrievalCandidate, RetrievalChannel
from app.retrieval.bm25_retriever import BM25Retriever
from app.retrieval.embedder import Embedder
from app.retrieval.vector_retriever import EmbeddingVectorRetriever


class HybridRetriever:
    """以 RRF 融合独立词法/向量候选，并可在候选集上执行精排。"""

    def __init__(
        self,
        bm25_weight: float,
        vector_weight: float,
        embedder: Embedder,
        reranker=None,
        candidate_k: int | None = None,
    ) -> None:
        """保留旧权重入参兼容构造器；默认融合算法改为分数无关的 RRF。"""
        self.bm25 = BM25Retriever()
        self.vector = EmbeddingVectorRetriever(embedder)
        self.bm25_weight = bm25_weight
        self.vector_weight = vector_weight
        self.reranker = reranker
        self.candidate_k = candidate_k

    def search(
        self, query: str, chunks: list[Chunk], top_k: int, *, rerank: bool = True
    ) -> list[RetrievalCandidate]:
        """并行语义/词法召回后按 RRF 合并；精排不突破候选边界。"""
        candidate_k = max(top_k, self.candidate_k or top_k * 4)
        bm25_hits = self.bm25.search(query, chunks, candidate_k)
        vector_hits = self.vector.search(query, chunks, candidate_k)
        candidates = self._rrf(bm25_hits, vector_hits)
        if rerank:
            candidates = self.rerank(query, candidates, min(candidate_k, len(candidates)))
        return candidates[:top_k]

    def rerank(
        self, query: str, candidates: list[RetrievalCandidate], top_k: int
    ) -> list[RetrievalCandidate]:
        """Apply the configured reranker only when the resolved profile permits it."""

        if self.reranker is None:
            return candidates[:top_k]
        return self.reranker.rerank(query, candidates, top_k)

    @staticmethod
    def _rrf(
        lexical: list[RetrievalCandidate], dense: list[RetrievalCandidate], *, k: int = 60
    ) -> list[RetrievalCandidate]:
        """用 Reciprocal Rank Fusion 合并异构排序，避免比较 BM25 与余弦分数。"""
        merged: dict[str, RetrievalCandidate] = {}
        channels: dict[str, set[RetrievalChannel]] = {}
        for hits in (lexical, dense):
            for rank, hit in enumerate(hits, start=1):
                key = hit.chunk_id or f"{hit.source_id}:{hit.metadata.get('start', 0)}"
                score = 1.0 / (k + rank)
                if key not in merged:
                    merged[key] = hit.model_copy(update={"score": score})
                    channels[key] = {hit.channel}
                else:
                    merged[key].score += score
                    channels[key].add(hit.channel)
        ranked = sorted(merged.values(), key=lambda item: (-item.score, item.candidate_id))
        result: list[RetrievalCandidate] = []
        for rank, item in enumerate(ranked, start=1):
            key = item.chunk_id or f"{item.source_id}:{item.metadata.get('start', 0)}"
            result.append(
                item.model_copy(
                    update={
                        "rank": rank,
                        "channel": RetrievalChannel.HYBRID,
                        "metadata": {
                            **item.metadata,
                            "retrieval_channels": sorted(channel.value for channel in channels[key]),
                            "fusion": "RRF",
                            "fusion_revision": "rrf/v1",
                        },
                    }
                )
            )
        return result
