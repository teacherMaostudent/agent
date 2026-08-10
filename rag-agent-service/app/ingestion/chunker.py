from app.domain.models import Chunk


class TextChunker:
    def __init__(self, chunk_size: int = 700, overlap: int = 120) -> None:
        self.chunk_size = chunk_size
        self.overlap = overlap

    def chunk(
        self, source_id: str, source_type: str, text: str, metadata: dict | None = None
    ) -> list[Chunk]:
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
