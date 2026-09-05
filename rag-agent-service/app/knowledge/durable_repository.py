"""Durable document facade with an in-memory read model.

Persistence is written before the document is returned, while the local cache
is rebuilt defensively.  Invalid historical data is skipped rather than making
the entire RAG process unavailable at start-up.
"""

from __future__ import annotations

import logging

from app.contracts.rag import IndexBuildManifest
from app.domain.models import Document
from app.knowledge.repository import InMemoryRepository
from pydantic import ValidationError

_KIND_DOCUMENT = "document"
_KIND_INDEX_MANIFEST = "index-build-manifest"
log = logging.getLogger(__name__)


class DurableRepository(InMemoryRepository):
    """以 KV 为事实来源、以内存为读缓存的持久化文档仓储。"""

    def __init__(self, kv) -> None:
        """先绑定持久化后端再恢复有效记录；损坏历史记录不阻塞服务启动。"""
        super().__init__()
        self._kv = kv
        self._load_all()

    def _load_all(self) -> None:
        """恢复校验通过的持久化文档；损坏记录只告警忽略，不阻断服务启动。"""
        for payload in self._kv.all(_KIND_DOCUMENT):
            try:
                document = Document(**payload)
                self.documents[document.document_id] = document
            except ValidationError:
                log.warning("invalid persisted document ignored")
        for payload in self._kv.all(_KIND_INDEX_MANIFEST):
            try:
                manifest = IndexBuildManifest.model_validate(payload)
                self.index_manifests[manifest.manifest_id] = manifest
            except ValidationError:
                log.warning("invalid persisted index manifest ignored")

    def save_document(self, document: Document) -> Document:
        """先更新读模型并同步写入 KV；索引仍由独立摄取投影负责。"""
        super().save_document(document)
        self._kv.put(_KIND_DOCUMENT, document.document_id, document.model_dump(mode="json"))
        return document

    def get_document(self, document_id: str) -> Document | None:
        """从 KV 重新读取权威快照并刷新缓存，避免依赖过期内存状态。"""
        payload = self._kv.get(_KIND_DOCUMENT, document_id)
        if payload is None:
            return None
        document = Document(**payload)
        self.documents[document.document_id] = document
        return document

    def list_documents(self, tenant_id: str, *, knowledge_base: str = "") -> list[Document]:
        """Read current durable documents, not only the process-local cache, for rebuilds."""

        documents: list[Document] = []
        for payload in self._kv.all(_KIND_DOCUMENT):
            try:
                document = Document(**payload)
            except ValidationError:
                log.warning("invalid persisted document ignored during list")
                continue
            self.documents[document.document_id] = document
            if document.metadata.get("tenant_id") == tenant_id and (
                not knowledge_base
                or document.metadata.get("knowledge_base", "default") == knowledge_base
            ):
                documents.append(document)
        return documents

    def document_chunks(self, document_id: str):
        """从权威文档文本重建检索片段；空文本自然产生空集合。"""
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
        """Write a build manifest before it can be referenced by a Retrieval Release."""

        super().save_index_manifest(manifest)
        self._kv.put(
            _KIND_INDEX_MANIFEST,
            manifest.manifest_id,
            manifest.model_dump(mode="json"),
        )
        return manifest

    def get_index_manifest(self, manifest_id: str) -> IndexBuildManifest | None:
        """Reload the persisted manifest, preventing stale in-memory publish decisions."""

        payload = self._kv.get(_KIND_INDEX_MANIFEST, manifest_id)
        if payload is None:
            return None
        manifest = IndexBuildManifest.model_validate(payload)
        self.index_manifests[manifest.manifest_id] = manifest
        return manifest

    def close(self) -> None:
        """关闭底层 KV 后端。"""
        self._kv.close()
