import hashlib
import math
from typing import Protocol

from app.domain.models import Chunk, RetrievalCandidate, RetrievalChannel
from app.retrieval.tokenizer import tokenize


class HashEmbeddingRetriever:
    """Deterministic local embedding stand-in; replace with Qwen/bge embedding later."""

    def __init__(self, dim: int = 384) -> None:
        """固定可复现向量维度；仅用于离线/测试回退，不代表生产语义模型。"""
        self.dim = dim

    def search(self, query: str, chunks: list[Chunk], top_k: int) -> list[RetrievalCandidate]:
        """按确定性哈希向量计算余弦相似度，返回正分候选而不改变输入片段。"""
        query_vec = self._embed(query)
        scored = []
        for chunk in chunks:
            score = self._cosine(query_vec, self._embed(chunk.text))
            if score > 0:
                scored.append((score, chunk))
        return [
            RetrievalCandidate(
                chunk_id=chunk.chunk_id,
                document_id=str(chunk.metadata.get("document_id", "")),
                document_version=str(chunk.metadata.get("document_version", "")),
                source_id=chunk.source_id,
                source_type=chunk.source_type,
                text=chunk.text,
                score=score,
                channel=RetrievalChannel.DENSE,
                rank=rank,
                metadata=chunk.metadata,
            )
            for rank, (score, chunk) in enumerate(
                sorted(scored, reverse=True, key=lambda item: item[0])[:top_k], start=1
            )
        ]

    def _embed(self, text: str) -> list[float]:
        """把 token 映射到稳定哈希桶，保证同一版本下索引和查询向量可比较。"""
        vector = [0.0] * self.dim
        for token in tokenize(text):
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            index = int.from_bytes(digest[:4], "big") % self.dim
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            vector[index] += sign
        return vector

    @staticmethod
    def _cosine(a: list[float], b: list[float]) -> float:
        """计算余弦相似度；空向量返回零，避免除零并避免制造假相关。"""
        dot = sum(x * y for x, y in zip(a, b, strict=False))
        norm_a = math.sqrt(sum(x * x for x in a))
        norm_b = math.sqrt(sum(x * x for x in b))
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)


class _Embedder(Protocol):
    """向量检索所需的最小嵌入接口，避免反向依赖 RAG 容器。"""

    def embed(self, text: str) -> list[float]:
        """把查询或片段映射到同一已发布向量空间。"""
        ...


class EmbeddingVectorRetriever:
    """使用统一 Provider 做本地向量召回，不再在查询路径私自实例化 Hash 模型。"""

    def __init__(self, embedder: _Embedder) -> None:
        """保存启动期创建的 Provider；摄取与查询容器必须由相同配置构造它。"""
        self._embedder = embedder

    def search(self, query: str, chunks: list[Chunk], top_k: int) -> list[RetrievalCandidate]:
        """用同一嵌入契约计算余弦候选，维度不匹配时直接失败而不返回伪相关结果。"""
        query_vec = self._embedder.embed(query)
        scored: list[tuple[float, Chunk]] = []
        for chunk in chunks:
            vector = self._embedder.embed(chunk.text)
            if len(vector) != len(query_vec):
                raise ValueError("embedding provider returned inconsistent vector dimension")
            score = HashEmbeddingRetriever._cosine(query_vec, vector)
            if score > 0:
                scored.append((score, chunk))
        return [
            RetrievalCandidate(
                chunk_id=chunk.chunk_id,
                document_id=str(chunk.metadata.get("document_id", "")),
                document_version=str(chunk.metadata.get("document_version", "")),
                source_id=chunk.source_id,
                source_type=chunk.source_type,
                text=chunk.text,
                score=score,
                channel=RetrievalChannel.DENSE,
                rank=rank,
                metadata=chunk.metadata,
            )
            for rank, (score, chunk) in enumerate(
                sorted(scored, reverse=True, key=lambda item: item[0])[:top_k], start=1
            )
        ]
