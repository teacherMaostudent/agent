"""Cross-service contracts for approved Artifact-to-RAG promotion."""

from __future__ import annotations

from pydantic import BaseModel, Field


class ApprovedArtifactIngestion(BaseModel):
    """Immutable Context Artifact reference after an explicit human decision."""

    artifact_id: str = Field(min_length=1, max_length=160)
    root_task_id: str = Field(min_length=1, max_length=160)
    content_ref: str = Field(min_length=1, max_length=2_000)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    media_type: str = Field(default="application/json", max_length=160)
    logical_name: str = Field(default="desktop-scan-result", max_length=160)
    approval_id: str = Field(min_length=1, max_length=160)
    approved_by: str = Field(min_length=1, max_length=160)


class ArtifactIngestionReceipt(BaseModel):
    """Stable identifiers returned by the ingestion control plane."""

    artifact_id: str
    document_id: str
    job_id: str
    status: str
