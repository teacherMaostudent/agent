"""Cross-service contracts for approved Artifact-to-RAG promotion."""

from __future__ import annotations

from datetime import datetime

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


class ApprovedWikiPageIngestion(BaseModel):
    """Human-approved Wiki page content promoted into the RAG document authority."""

    page_id: str = Field(min_length=1, max_length=160)
    candidate_id: str = Field(min_length=1, max_length=160)
    version: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=200)
    markdown: str = Field(min_length=1, max_length=200_000)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    approved_by: str = Field(min_length=1, max_length=160)
    source_ids: list[str] = Field(min_length=1, max_length=200)
    valid_until: datetime | None = None
    supersedes_page_ids: list[str] = Field(default_factory=list, max_length=100)


class WikiPageIngestionReceipt(BaseModel):
    """Stable document and parse job identifiers for one approved Wiki version."""

    page_id: str
    version: int
    document_id: str
    job_id: str
    status: str
