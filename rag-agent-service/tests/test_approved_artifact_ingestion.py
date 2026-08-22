"""Contract tests for the human-approved Desktop Artifact promotion boundary."""

from types import SimpleNamespace

from platform_sdk.contracts.ingestion import ApprovedArtifactIngestion

from app.ingestion.job_store import IngestionJobStore
from app.ingestion.parsers import DocumentParser
from app.ingestion.processor import IngestionJobProcessor
from app.knowledge.repository import InMemoryRepository
from app.service_api.ingestion_api import ingest_approved_artifact


def test_approved_artifact_ingestion_is_scoped_and_idempotent(tmp_path):
    """Relay retries must resolve to one document/job and preserve approval provenance."""
    objects = SimpleNamespace(bucket="platform-artifacts", prefix="agent-platform")

    class Storage:
        """Materialize the approved object as a controlled-scan JSON result."""

        def __init__(self):
            self.objects = objects

        def materialize(self, path, metadata):
            assert metadata["object_key"].startswith("agent-platform/")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('{"matches":[{"path":"README.md","line":7}]}', encoding="utf-8")
            return path

    indexed: list[str] = []
    projection = SimpleNamespace(
        index_document=lambda document, _chunks: indexed.append(document.document_id)
    )
    container = SimpleNamespace(
        storage=Storage(),
        repository=InMemoryRepository(),
        job_store=IngestionJobStore(tmp_path / "jobs.db"),
        settings=SimpleNamespace(upload_dir=tmp_path / "uploads"),
        parser=DocumentParser(),
        search_projection=projection,
    )
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(container=container)))
    payload = ApprovedArtifactIngestion(
        artifact_id="art-1",
        root_task_id="root-1",
        content_ref="s3://platform-artifacts/agent-platform/tasks/tenant-a/root-1/scan.json",
        content_sha256="a" * 64,
        approval_id="decision-1",
        approved_by="reviewer-1",
    )

    first = ingest_approved_artifact(payload, request, "tenant-a", "relay-user")
    second = ingest_approved_artifact(payload, request, "tenant-a", "relay-user")

    assert first == second
    document = container.repository.get_document(first.document_id)
    assert document is not None
    assert document.metadata["approval_id"] == "decision-1"
    assert document.metadata["approved_by"] == "reviewer-1"
    assert document.metadata["tenant_id"] == "tenant-a"
    claimed = container.job_store.claim_next()
    assert claimed is not None
    result = IngestionJobProcessor(container).process(claimed)
    container.job_store.complete(claimed, result)
    assert container.job_store.get(first.job_id, "tenant-a").status.value == "COMPLETED"
    assert indexed == [first.document_id]
    container.job_store.close()
