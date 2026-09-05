import math
from collections import Counter

from app.domain.models import Chunk, RetrievalCandidate, RetrievalChannel
from app.retrieval.tokenizer import tokenize


class BM25Retriever:
    def search(self, query: str, chunks: list[Chunk], top_k: int) -> list[RetrievalCandidate]:
        """以词项统计召回候选片段；该层只计算相关性，不负责 ACL 或最终决策。"""
        query_terms = tokenize(query)
        if not query_terms or not chunks:
            return []

        tokenized = [tokenize(chunk.text) for chunk in chunks]
        avg_len = sum(len(tokens) for tokens in tokenized) / max(len(tokenized), 1)
        doc_freq = Counter(term for tokens in tokenized for term in set(tokens))
        scores: list[tuple[float, Chunk]] = []
        for chunk, tokens in zip(chunks, tokenized, strict=False):
            counts = Counter(tokens)
            score = 0.0
            for term in query_terms:
                if counts[term] == 0:
                    continue
                idf = math.log(1 + (len(chunks) - doc_freq[term] + 0.5) / (doc_freq[term] + 0.5))
                numerator = counts[term] * 2.2
                denominator = counts[term] + 1.2 * (0.25 + 0.75 * len(tokens) / max(avg_len, 1))
                score += idf * numerator / denominator
            if score > 0:
                scores.append((score, chunk))
        return [
            _to_candidate(chunk, score, rank)
            for rank, (score, chunk) in enumerate(
                sorted(scores, reverse=True, key=lambda item: item[0])[:top_k], start=1
            )
        ]


def _to_candidate(chunk: Chunk, score: float, rank: int) -> RetrievalCandidate:
    """表达词法相关候选；最终 Evidence 只能由验证器创建。"""
    return RetrievalCandidate(
        chunk_id=chunk.chunk_id,
        document_id=str(chunk.metadata.get("document_id", "")),
        document_version=str(chunk.metadata.get("document_version", "")),
        source_id=chunk.source_id,
        source_type=chunk.source_type,
        text=chunk.text,
        score=score,
        channel=RetrievalChannel.LEXICAL,
        rank=rank,
        metadata=chunk.metadata,
    )
