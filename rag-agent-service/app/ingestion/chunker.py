from app.domain.models import Chunk


class TextChunker:
    """将解析后的文本切为可追溯、可重建的检索片段。"""

    def __init__(self, chunk_size: int = 700, overlap: int = 120) -> None:
        """固定片段窗口与重叠区，避免召回跨边界语义时丢失上下文。"""
        self.chunk_size = chunk_size
        self.overlap = overlap

    def chunk(
        self, source_id: str, source_type: str, text: str, metadata: dict | None = None
    ) -> list[Chunk]:
        """规范化文本并生成带字符偏移量的片段，不写入任何持久化状态。"""
        clean = "\n".join(line.strip() for line in text.splitlines() if line.strip())
        if not clean:
            return []
        chunks: list[Chunk] = []
        start = 0
        while start < len(clean):
            end = min(start + self.chunk_size, len(clean))
            chunks.append(
                Chunk(
                    source_id=source_id,
                    source_type=source_type,
                    text=clean[start:end],
                    metadata={**(metadata or {}), "start": start, "end": end},
                )
            )
            if end == len(clean):
                break
            start = max(0, end - self.overlap)
        return chunks
