"""Small generic document repository used by local development adapters."""

from app.domain.models import Chunk, Document
from app.ingestion.chunker import TextChunker


class InMemoryRepository:
    """Stores tenant-scoped enterprise documents without domain seed data."""

    def __init__(self) -> None:
        self.documents: dict[str, Document] = {}
        self._chunker = TextChunker()

    def save_document(self, document: Document) -> Document:
        self.documents[document.document_id] = document
        return document

    def get_document(self, document_id: str) -> Document | None:
        return self.documents.get(document_id)

    def document_chunks(self, document_id: str) -> list[Chunk]:
        document = self.get_document(document_id)
        if document is None:
            return []
        return self._chunker.chunk(
            document.document_id,
            "enterprise_document",
            document.text,
            document.metadata,
        )
