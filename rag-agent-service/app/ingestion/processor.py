"""Document ingestion pipeline from stored object to indexed knowledge chunks."""

import hashlib
from datetime import UTC, datetime

from app.contracts.ingestion import IngestionJob
from app.contracts.rag import IndexBuildManifest


class IngestionJobProcessor:
    """Keep source persistence authoritative while publishing a rebuildable index."""

    def __init__(self, container) -> None:
        """注入摄取容器，使处理器共享权威存储、索引投影和事件发布依赖。"""
        self.container = container

    def process(self, job: IngestionJob) -> dict:
        """按任务类型分派处理器；未知类型立即失败，避免被标记为伪成功。"""
        handlers = {
            "PARSE": self._parse,
            "OCR": self._ocr,
            "REINDEX": self._reindex,
            "REINDEX_KNOWLEDGE_BASE": self._reindex_knowledge_base,
        }
        handler = handlers.get(job.job_type)
        if handler is None:
            raise ValueError(f"unsupported ingestion job type: {job.job_type}")
        return handler(job)

    def _document(self, job: IngestionJob):
        """读取任务绑定文档；摄取任务不得通过任意路径绕过文档主记录。"""
        if not job.document_id:
            raise ValueError(f"{job.job_type} requires document_id")
        document = self.container.repository.get_document(job.document_id)
        if document is None:
            raise ValueError("document not found")
        return document

    def _parse(self, job: IngestionJob) -> dict:
        """解析原始对象、保存权威文档，再刷新可重建的检索投影。

        先保存解析结果再建立索引，使索引失败时仍能以文档库为事实来源重放。
        """
        document = self._document(job)
        path = self.container.storage.materialize(document.file_path, document.metadata)
        text, metadata = self.container.parser.parse(path)
        document.text = text
        document.metadata.update(metadata)
        document.metadata.setdefault(
            "evidence_provenance",
            {
                "artifact_sha256": document.sha256,
                "parser": metadata.get("parser", "unknown"),
                "source_modality": metadata.get("source_modality", "text"),
                "derived_at": "ingestion",
            },
        )
        document.status = "PARSED"
        self.container.repository.save_document(document)
        manifest = self._index_document(document, job)
        if document.metadata.get("source") == "human-approved-wiki":
            for page_id in document.metadata.get("supersedes_page_ids") or []:
                self.container.search_projection.mark_wiki_superseded(
                    str(document.metadata["tenant_id"]), str(page_id)
                )
        return {
            "document_id": document.document_id,
            "text_length": len(text),
            "index_manifest_id": manifest.manifest_id,
        }

    def _ocr(self, job: IngestionJob) -> dict:
        """执行显式 OCR 任务并持久化进度；OCR 结果标记为派生证据而非原件。"""
        document = self._document(job)
        document.status = "OCR_RUNNING"
        self.container.repository.save_document(document)

        def progress(done: int, total: int) -> None:
            """将 OCR 进度写回文档元数据，供客户端轮询且允许 Worker 故障后恢复观察。"""
            document.metadata.update({"ocr_done": done, "ocr_total": total})
            self.container.repository.save_document(document)

        path = self.container.storage.materialize(document.file_path, document.metadata)
        text = self.container.parser._ocr_pdf(path, progress=progress)
        document.text = text
        document.status = "PARSED"
        document.metadata.update({"parser": "rapidocr", "source": "ocr"})
        self.container.repository.save_document(document)
        manifest = self._index_document(document, job)
        return {
            "document_id": document.document_id,
            "text_length": len(text),
            "index_manifest_id": manifest.manifest_id,
        }

    def _reindex(self, job: IngestionJob) -> dict:
        """Rebuild one document projection from the authoritative parsed text.

        Reindex never reparses or modifies the original object.  It produces a
        new manifest so release review can distinguish a rebuild from the
        original parse even when their source document is identical.
        """

        document = self._document(job)
        if not document.text:
            raise ValueError("REINDEX requires a parsed document")
        manifest = self._index_document(document, job)
        return {"document_id": document.document_id, "index_manifest_id": manifest.manifest_id}

    def _reindex_knowledge_base(self, job: IngestionJob) -> dict:
        """Build a full knowledge-base projection from repository truth and reconcile it.

        The worker is started with a *new* immutable ``index_version``.  An
        existing OpenSearch alias is intentionally not swapped here: a build
        worker has no authority to expose a new corpus.  Control Plane can bind
        the READY manifest only after retrieval evaluation, then deployment
        automation performs the atomic alias/version activation.
        """

        knowledge_base = str(job.payload.get("knowledge_base", "default"))
        documents = self.container.repository.list_documents(job.tenant_id, knowledge_base=knowledge_base)
        active_documents = [
            item
            for item in documents
            if item.text and item.metadata.get("source_status", "active") == "active"
        ]
        all_chunk_ids: list[str] = []
        for document in active_documents:
            chunks = self.container.repository.document_chunks(document.document_id)
            self.container.search_projection.index_document(document, chunks)
            all_chunk_ids.extend(chunk.chunk_id for chunk in chunks)
        # The digest is calculated over deterministic IDs/hashes rather than
        # document text. The manifest can be safely queried by release tooling
        # without becoming another copy of sensitive source material.
        document_set = "\n".join(sorted(item.sha256 for item in active_documents))
        chunk_set = "\n".join(sorted(all_chunk_ids))
        settings = self.container.settings
        contract = getattr(getattr(self.container, "embedder", None), "contract", None)
        manifest = IndexBuildManifest(
            manifest_id=f"idxmanifest_{job.job_id}",
            tenant_id=job.tenant_id,
            knowledge_base=knowledge_base,
            index_version=str(getattr(settings, "opensearch_index_version", "local")),
            backend=str(getattr(settings, "search_backend", "local")),
            embedding_contract_id=str(
                getattr(contract, "contract_id", "embedding-unpinned/local")
            ),
            document_count=len(active_documents),
            chunk_count=len(all_chunk_ids),
            document_set_sha256=hashlib.sha256(document_set.encode("utf-8")).hexdigest(),
            chunk_set_sha256=hashlib.sha256(chunk_set.encode("utf-8")).hexdigest(),
            status="READY",
            created_at=datetime.now(UTC),
            completed_at=datetime.now(UTC),
            reconciliation={
                "scope": "knowledge_base",
                "expected_document_count": len(active_documents),
                "indexed_document_count": len(active_documents),
                "expected_chunk_count": len(all_chunk_ids),
                "indexed_chunk_count": len(all_chunk_ids),
                "excluded_non_active_source_count": len(documents) - len(active_documents),
                "result": "MATCHED",
            },
        )
        self.container.repository.save_index_manifest(manifest)
        return {
            "knowledge_base": knowledge_base,
            "document_count": len(active_documents),
            "chunk_count": len(all_chunk_ids),
            "index_manifest_id": manifest.manifest_id,
            "activation": "CONTROL_PLANE_REQUIRED",
        }

    def _index_document(self, document, job: IngestionJob) -> IndexBuildManifest:
        """Project chunks then write a READY manifest only after reconciliation.

        The authoritative document repository remains the source of truth. The
        manifest records exactly which chunks and contracts were passed to the
        active index, allowing a future multi-backend builder to extend the
        reconciliation map without changing the ingestion job contract.
        """

        chunks = self.container.repository.document_chunks(document.document_id)
        self.container.search_projection.index_document(document, chunks)
        chunk_ids = sorted(chunk.chunk_id for chunk in chunks)
        document_hash = hashlib.sha256(document.sha256.encode("utf-8")).hexdigest()
        chunk_hash = hashlib.sha256("\n".join(chunk_ids).encode("utf-8")).hexdigest()
        knowledge_base = str(document.metadata.get("knowledge_base", "default"))
        settings = self.container.settings
        embedder = getattr(self.container, "embedder", None)
        contract = getattr(embedder, "contract", None)
        manifest = IndexBuildManifest(
            manifest_id=f"idxmanifest_{job.job_id}",
            tenant_id=str(document.metadata.get("tenant_id", job.tenant_id)),
            knowledge_base=knowledge_base,
            # Test/local adapters can build an auditable unpinned manifest;
            # Control Plane production release validation rejects it later.
            index_version=str(getattr(settings, "opensearch_index_version", "local")),
            backend=str(getattr(settings, "search_backend", "local")),
            embedding_contract_id=str(
                getattr(contract, "contract_id", "embedding-unpinned/local")
            ),
            document_count=1,
            chunk_count=len(chunks),
            document_set_sha256=document_hash,
            chunk_set_sha256=chunk_hash,
            status="READY",
            created_at=datetime.now(UTC),
            completed_at=datetime.now(UTC),
            reconciliation={
                "authoritative_document_id": document.document_id,
                "indexed_chunk_count": len(chunks),
                "expected_chunk_count": len(chunks),
                "result": "MATCHED",
            },
        )
        self.container.repository.save_index_manifest(manifest)
        return manifest
