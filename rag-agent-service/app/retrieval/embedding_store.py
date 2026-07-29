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
    def __init__(self) -> None:
        self._chunks: list[Chunk] = []
        self._vectors: list[list[float]] = []
        # 建这个库用的 embedder 名(hash/qwen)。检索时须与当前 embedder 一致，
        # 否则查询向量和库向量不同源、维度不符，相似度全是垃圾值。
        self.embedder_name: str = ""

    @property
    def dim(self) -> int:
        return len(self._vectors[0]) if self._vectors else 0

    def __len__(self) -> int:
        return len(self._chunks)

    def add(self, chunks: list[Chunk], vectors: list[list[float]], embedder_name: str = "") -> None:
        if len(chunks) != len(vectors):
            raise ValueError("chunks 与 vectors 数量不一致")
        self._chunks.extend(chunks)
        self._vectors.extend(vectors)
        if embedder_name:
            self.embedder_name = embedder_name

    def clear(self) -> None:
        self._chunks = []
        self._vectors = []
        self.embedder_name = ""

    def search(
        self,
        query_vector: list[float],
        top_k: int,
        metadata_filter: dict | None = None,
    ) -> list[Evidence]:
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
        return all(chunk.metadata.get(k) == v for k, v in metadata_filter.items())

    @staticmethod
    def _cosine(a: list[float], b: list[float]) -> float:
        dot = sum(x * y for x, y in zip(a, b, strict=False))
        na = math.sqrt(sum(x * x for x in a))
        nb = math.sqrt(sum(x * x for x in b))
        if na == 0 or nb == 0:
            return 0.0
        return dot / (na * nb)

    # --- 持久化:embedding 建一次存本地，重启直接加载 ---

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "embedder": self.embedder_name,
            "chunks": [chunk.model_dump(mode="json") for chunk in self._chunks],
            "vectors": self._vectors,
        }
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def load(self, path: Path) -> bool:
        if not path.exists():
            return False
        data = json.loads(path.read_text(encoding="utf-8"))
        self.embedder_name = data.get("embedder", "")
        self._chunks = [Chunk(**c) for c in data.get("chunks", [])]
        self._vectors = [list(map(float, v)) for v in data.get("vectors", [])]
        return True
