"""Source revocation must update truth before derived retrieval projections."""

from pathlib import Path
from types import SimpleNamespace

import pytest
from app.domain.models import Document
from app.knowledge.repository import InMemoryRepository
from app.service_api.ingestion_api import update_source_status
from fastapi import HTTPException


class _Projection:
    """Records the same source event the OpenSearch adapter would receive."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    def update_source_status(self, tenant_id: str, source_id: str, status: str) -> int:
        """Return a deterministic affected-chunk count for this API boundary test."""
        self.calls.append((tenant_id, source_id, status))
        return 2


def _request(repository, projection):
    """Create the smallest request shell consumed by the FastAPI route function."""
    return SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                container=SimpleNamespace(repository=repository, search_projection=projection)
            )
        )
    )


def test_source_revocation_updates_repository_then_projection() -> None:
    """Revocation is tenant-scoped and leaves a durable reason for audit/re-ingestion."""
    repository = InMemoryRepository()
    repository.save_document(
        Document(
            document_id="doc-1",
            filename="source.txt",
            file_path=Path("source.txt"),
            sha256="a" * 64,
            metadata={"tenant_id": "tenant-a", "source_id": "crm-export"},
        )
    )
    projection = _Projection()

    result = update_source_status(
        "crm-export",
        {"status": "revoked", "reason": "upstream deletion event"},
        _request(repository, projection),
        x_tenant_id="tenant-a",
        x_permissions="rag:source:revoke",
    )

    assert result["authoritative_documents_updated"] == 1
    assert result["indexed_chunks_updated"] == 2
    assert repository.get_document("doc-1").metadata["source_status"] == "revoked"
    assert projection.calls == [("tenant-a", "crm-export", "revoked")]


def test_source_revocation_requires_dedicated_permission() -> None:
    """A generic ingestion caller cannot quietly revoke another source's evidence."""
    with pytest.raises(HTTPException, match="rag:source:revoke"):
        update_source_status(
            "crm-export",
            {"status": "revoked"},
            _request(InMemoryRepository(), _Projection()),
            x_permissions="rag:ingest",
        )
