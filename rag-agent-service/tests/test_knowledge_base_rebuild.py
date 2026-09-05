"""Full knowledge-base rebuilds must produce a reconciled, non-activated manifest."""

from pathlib import Path
from types import SimpleNamespace

from app.contracts.ingestion import IngestionJob
from app.domain.models import Document
from app.ingestion.processor import IngestionJobProcessor
from app.knowledge.repository import InMemoryRepository


class _Projection:
    """Captures documents sent to an otherwise external versioned index."""

    def __init__(self) -> None:
        self.indexed: list[str] = []

    def index_document(self, document, chunks) -> None:
        """Record chunk-producing documents while preserving the processor's call shape."""
        assert chunks
        self.indexed.append(document.document_id)


def test_knowledge_base_rebuild_reconciles_active_sources_without_activation() -> None:
    """Revoked sources are excluded and build completion does not publish an alias."""
    repository = InMemoryRepository()
    for document_id, source_status in (("doc-active", "active"), ("doc-revoked", "revoked")):
        repository.save_document(
            Document(
                document_id=document_id,
                filename=f"{document_id}.txt",
                file_path=Path(f"{document_id}.txt"),
                sha256=("a" if source_status == "active" else "b") * 64,
                text="approved knowledge",
                metadata={
                    "tenant_id": "tenant-a",
                    "knowledge_base": "policies",
                    "source_status": source_status,
                },
            )
        )
    projection = _Projection()
    container = SimpleNamespace(
        repository=repository,
        search_projection=projection,
        settings=SimpleNamespace(opensearch_index_version="v-next", search_backend="opensearch"),
        embedder=SimpleNamespace(contract=SimpleNamespace(contract_id="emb-next")),
    )
    result = IngestionJobProcessor(container).process(
        IngestionJob(
            job_id="job-rebuild-1",
            job_type="REINDEX_KNOWLEDGE_BASE",
            tenant_id="tenant-a",
            payload={"knowledge_base": "policies"},
        )
    )

    manifest = repository.get_index_manifest(result["index_manifest_id"])
    assert projection.indexed == ["doc-active"]
    assert manifest is not None and manifest.status == "READY"
    assert manifest.reconciliation["excluded_non_active_source_count"] == 1
    assert result["activation"] == "CONTROL_PLANE_REQUIRED"
