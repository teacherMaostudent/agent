import math
from collections import Counter

from app.domain.models import Chunk, Evidence
from app.retrieval.tokenizer import tokenize


class BM25Retriever:
    def search(self, query: str, chunks: list[Chunk], top_k: int) -> list[Evidence]:
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
            _to_evidence(chunk, score)
            for score, chunk in sorted(scores, reverse=True, key=lambda item: item[0])[:top_k]
        ]


def _to_evidence(chunk: Chunk, score: float) -> Evidence:
    """将内部片段转换为统一证据契约，保留原始元数据以支持后续引用和审计。"""
    return Evidence(
        source_id=chunk.source_id,
        source_type=chunk.source_type,
        text=chunk.text,
        score=score,
        metadata=chunk.metadata,
    )
