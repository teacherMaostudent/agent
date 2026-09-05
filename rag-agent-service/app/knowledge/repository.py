"""Small generic document repository used by local development adapters."""

from app.contracts.rag import IndexBuildManifest
from app.domain.models import Chunk, Document
from app.ingestion.chunker import TextChunker


class InMemoryRepository:
    """Stores tenant-scoped enterprise documents without domain seed data."""

    def __init__(self) -> None:
        """初始化仅供测试/开发的内存读模型，不提供跨进程一致性。"""
        self.documents: dict[str, Document] = {}
        # Manifests are control records, not searchable source data.  They
        # make index publication inspectable without placing vectors in KV.
        self.index_manifests: dict[str, IndexBuildManifest] = {}
        self._chunker = TextChunker()

    def save_document(self, document: Document) -> Document:
        """写入权威文档对象；ACL 元数据由 API 在创建时附着。"""
        self.documents[document.document_id] = document
        return document

    def get_document(self, document_id: str) -> Document | None:
        """按 ID 读取文档；调用方必须在返回前执行租户 ACL 校验。"""
        return self.documents.get(document_id)

    def list_documents(self, tenant_id: str, *, knowledge_base: str = "") -> list[Document]:
        """List authoritative documents for one tenant and optional knowledge-base rebuild.

        This is intentionally a repository operation rather than an index scan:
        an index may be stale, partially rebuilt, or already contain revoked
        records and therefore cannot be the source for a reconciliation build.
        """

        return [
            document
            for document in self.documents.values()
            if document.metadata.get("tenant_id") == tenant_id
            and (
                not knowledge_base
                or document.metadata.get("knowledge_base", "default") == knowledge_base
            )
        ]

    def set_source_status(
        self, tenant_id: str, source_id: str, status: str, *, reason: str = ""
    ) -> list[Document]:
        """Persist a source lifecycle change before its projections are deactivated.

        ``source_id`` identifies the upstream system/object owner, not a
        Runtime user id.  Existing legacy records without source_id are not
        guessed or mutated; callers receive an empty result and can repair
        provenance through a controlled re-ingestion job.
        """

        changed: list[Document] = []
        for document in self.documents.values():
            if (
                document.metadata.get("tenant_id") != tenant_id
                or document.metadata.get("source_id") != source_id
            ):
                continue
            document.metadata["source_status"] = status
            document.metadata["source_status_reason"] = reason
            self.save_document(document)
            changed.append(document)
        return changed

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

    def save_index_manifest(self, manifest: IndexBuildManifest) -> IndexBuildManifest:
        """Persist a build result by immutable ID; caller creates a new ID for rebuilds."""

        self.index_manifests[manifest.manifest_id] = manifest
        return manifest

    def get_index_manifest(self, manifest_id: str) -> IndexBuildManifest | None:
        """Return a build manifest without exposing document contents or vectors."""

        return self.index_manifests.get(manifest_id)

    def list_index_manifests(
        self, tenant_id: str, knowledge_base: str, *, limit: int = 100
    ) -> list[IndexBuildManifest]:
        """List only tenant-owned manifests, newest first, for release validation."""

        items = [
            item
            for item in self.index_manifests.values()
            if item.tenant_id == tenant_id and item.knowledge_base == knowledge_base
        ]
        return sorted(items, key=lambda item: item.created_at, reverse=True)[:limit]
