"""向量存储 + 本地持久化缓存。

设计要点(呼应 Java 版 InMemoryEmbeddingStore + 本地 JSON):
- embedding 只在建库时算一次，存进本地 JSON；重启直接加载，不重算、不重复花钱。
- 检索时只 embed 查询本身，再和库里预存向量算余弦，避免"每次查询重算全库"。
- 支持按 metadata 过滤(如 standard/module)后再算相似度，减轻跨语言召回折损。
"""

import json
import math
from pathlib import Path

from app.domain.models import Chunk, Evidence


class EmbeddingStore:
    """保存片段及其同源向量的轻量存储；生产索引由 OpenSearch 投影承担。"""

    def __init__(self) -> None:
        """初始化空索引，并记录创建索引所用 embedder 以防混用向量空间。"""
        self._chunks: list[Chunk] = []
        self._vectors: list[list[float]] = []
        # 建这个库用的 embedder 名(hash/qwen)。检索时须与当前 embedder 一致，
        # 否则查询向量和库向量不同源、维度不符，相似度全是垃圾值。
        self.embedder_name: str = ""

    @property
    def dim(self) -> int:
        """返回当前索引向量维度；空索引返回零而非猜测模型默认维度。"""
        return len(self._vectors[0]) if self._vectors else 0

    def __len__(self) -> int:
        """返回可检索片段总数，供调用方判断索引是否具备查询条件。"""
        return len(self._chunks)

    def add(self, chunks: list[Chunk], vectors: list[list[float]], embedder_name: str = "") -> None:
        """追加等长片段和向量；不匹配立即拒绝，避免索引位置错位造成错误证据。"""
        if len(chunks) != len(vectors):
            raise ValueError("chunks 与 vectors 数量不一致")
        self._chunks.extend(chunks)
        self._vectors.extend(vectors)
        if embedder_name:
            self.embedder_name = embedder_name

    def clear(self) -> None:
        """清空可重建索引及其模型标识，不影响权威文档数据。"""
        self._chunks = []
        self._vectors = []
        self.embedder_name = ""

    def search(
        self,
        query_vector: list[float],
        top_k: int,
        metadata_filter: dict | None = None,
    ) -> list[Evidence]:
        """在可选元数据过滤后计算相似度；调用者需在此之前已完成 ACL 约束。"""
        scored: list[tuple[float, Chunk]] = []
        for chunk, vec in zip(self._chunks, self._vectors, strict=False):
            if metadata_filter and not self._matches(chunk, metadata_filter):
                continue
            score = self._cosine(query_vector, vec)
            if score > 0:
                scored.append((score, chunk))
        scored.sort(reverse=True, key=lambda item: item[0])
        return [
            Evidence(
                source_id=chunk.source_id,
                source_type=chunk.source_type,
                text=chunk.text,
                score=score,
                metadata=chunk.metadata,
            )
            for score, chunk in scored[:top_k]
        ]

    @staticmethod
    def _matches(chunk: Chunk, metadata_filter: dict) -> bool:
        """要求所有过滤字段严格匹配，避免宽松匹配混入错误业务域。"""
        return all(chunk.metadata.get(k) == v for k, v in metadata_filter.items())

    @staticmethod
    def _cosine(a: list[float], b: list[float]) -> float:
        """计算向量相似度；任一零向量无可用语义信号，返回零。"""
        dot = sum(x * y for x, y in zip(a, b, strict=False))
        na = math.sqrt(sum(x * x for x in a))
        nb = math.sqrt(sum(x * x for x in b))
        if na == 0 or nb == 0:
            return 0.0
        return dot / (na * nb)

    # --- 持久化:embedding 建一次存本地，重启直接加载 ---

    def save(self, path: Path) -> None:
        """持久化可重建索引缓存；文件必须与 embedder 版本一同管理。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "embedder": self.embedder_name,
            "chunks": [chunk.model_dump(mode="json") for chunk in self._chunks],
            "vectors": self._vectors,
        }
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def load(self, path: Path) -> bool:
        """加载本地缓存；不存在时返回 False，让调用方选择重建而非默默空检索。"""
        if not path.exists():
            return False
        data = json.loads(path.read_text(encoding="utf-8"))
        self.embedder_name = data.get("embedder", "")
        self._chunks = [Chunk(**c) for c in data.get("chunks", [])]
        self._vectors = [list(map(float, v)) for v in data.get("vectors", [])]
        return True
