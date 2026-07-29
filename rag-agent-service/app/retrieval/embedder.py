"""Embedder 抽象:统一"把文本转成向量"的接口,屏蔽 hash / qwen 差异。

- HashEmbedder:本地确定性 embedding(离线、免密钥、测试用),复用原 Hash 逻辑。
- QwenEmbedder:通义 text-embedding-v3(真实语义,建真法规库用)。
- build_embedder():按配置返回其一;qwen 缺密钥时自动回退 hash 并告警。

法规库建库和检索用同一个 embedder,保证查询向量和库向量同源可比。
"""
import hashlib
import logging

from app.core.config import Settings
from app.retrieval.qwen_embedding import QwenEmbeddingClient
from app.retrieval.tokenizer import tokenize

log = logging.getLogger(__name__)


class HashEmbedder:
    """确定性本地 embedding,与旧 HashEmbeddingRetriever 同算法,离线可用。"""

    name = "hash"

    def __init__(self, dim: int = 384) -> None:
        self.dim = dim

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [self._embed(t) for t in texts]

    def embed(self, text: str) -> list[float]:
        return self._embed(text)

    def _embed(self, text: str) -> list[float]:
        vector = [0.0] * self.dim
        for token in tokenize(text):
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            index = int.from_bytes(digest[:4], "big") % self.dim
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            vector[index] += sign
        return vector


class QwenEmbedder:
    """通义 text-embedding-v3 适配为统一接口。"""

    name = "qwen"

    def __init__(self, client: QwenEmbeddingClient) -> None:
        self.client = client

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return self.client.embed_batch(texts)

    def embed(self, text: str) -> list[float]:
        return self.client.embed(text)


def build_embedder(settings: Settings):
    """按配置返回 embedder。provider=qwen 但缺密钥时回退 hash 并告警。"""
    if settings.embedding_provider == "qwen":
        if not settings.dashscope_api_key:
            log.warning("embedding_provider=qwen 但未设 DASHSCOPE_API_KEY,回退 hash embedding")
            return HashEmbedder(settings.local_embedding_dim)
        client = QwenEmbeddingClient(
            api_key=settings.dashscope_api_key,
            base_url=settings.qwen_embedding_base_url,
            model=settings.qwen_embedding_model,
            batch_size=settings.qwen_embedding_batch_size,
        )
        return QwenEmbedder(client)
    return HashEmbedder(settings.local_embedding_dim)
