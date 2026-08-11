"""Small generic document repository used by local development adapters."""

from app.domain.models import Chunk, Document
from app.ingestion.chunker import TextChunker


class InMemoryRepository:
    """Stores tenant-scoped enterprise documents without domain seed data."""

    def __init__(self) -> None:
        """初始化仅供测试/开发的内存读模型，不提供跨进程一致性。"""
        self.documents: dict[str, Document] = {}
        self._chunker = TextChunker()

    def save_document(self, document: Document) -> Document:
        """写入权威文档对象；ACL 元数据由 API 在创建时附着。"""
        self.documents[document.document_id] = document
        return document

    def get_document(self, document_id: str) -> Document | None:
        """按 ID 读取文档；调用方必须在返回前执行租户 ACL 校验。"""
        return self.documents.get(document_id)

    def document_chunks(self, document_id: str) -> list[Chunk]:
        """从当前文档文本即时重建片段，避免把可再生数据当作主存储。"""
        document = self.get_document(document_id)
        if document is None:
            return []
        return self._chunker.chunk(
            document.document_id,
            "enterprise_document",
            document.text,
            document.metadata,
        )
