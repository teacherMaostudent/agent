import hashlib
from datetime import UTC, datetime

from app.contracts.rag import IndexBuildManifest
from app.knowledge.repository import InMemoryRepository


def test_index_manifest_is_tenant_scoped_and_retrievable() -> None:
    repository = InMemoryRepository()
    manifest = IndexBuildManifest(
        manifest_id="idxmanifest-1",
        tenant_id="tenant-a",
        knowledge_base="quality-kb",
        index_version="v7",
        backend="opensearch",
        embedding_contract_id="emb-7",
        document_count=1,
        chunk_count=2,
        document_set_sha256=hashlib.sha256(b"doc").hexdigest(),
        chunk_set_sha256=hashlib.sha256(b"chunks").hexdigest(),
        status="READY",
        created_at=datetime.now(UTC),
    )
    repository.save_index_manifest(manifest)

    assert repository.get_index_manifest("idxmanifest-1") == manifest
    assert repository.list_index_manifests("tenant-a", "quality-kb") == [manifest]
    assert repository.list_index_manifests("tenant-b", "quality-kb") == []
