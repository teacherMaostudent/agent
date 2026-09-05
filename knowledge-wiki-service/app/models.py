"""Stable contracts for compiled knowledge and its provenance graph."""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


def now() -> datetime:
    """Return timezone-aware UTC timestamps for cross-service ordering and expiry checks."""
    return datetime.now(UTC)


class StrictModel(BaseModel):
    """Reject unknown contract fields so typos cannot silently weaken governance metadata."""
    model_config = ConfigDict(extra="forbid")


class KnowledgeLevel(StrEnum):
    """Separate source facts, machine inference and explicit human confirmation."""
    RAW_EVIDENCE = "raw_evidence"
    MODEL_INFERENCE = "model_inference"
    HUMAN_CONFIRMED = "human_confirmed"


class CandidateStatus(StrEnum):
    """Model the single-consumption human review state machine."""
    PENDING_REVIEW = "pending_review"
    APPROVED = "approved"
    REJECTED = "rejected"


class RelationType(StrEnum):
    """Represent governed semantic links without overloading free-form tags."""
    LINKS_TO = "links_to"
    CONFLICTS_WITH = "conflicts_with"
    SUPERSEDES = "supersedes"
    DERIVED_FROM = "derived_from"


class KnowledgeSource(StrictModel):
    """Immutable source reference whose digest supports later provenance verification."""
    source_id: str = Field(min_length=1, max_length=200)
    source_type: str = Field(pattern="^(evidence|artifact|review|run)$")
    knowledge_level: KnowledgeLevel
    content_sha256: str = Field(pattern="^[a-f0-9]{64}$")
    uri: str = Field(default="", max_length=2_000)
    captured_at: datetime = Field(default_factory=now)


class WikiDraft(StrictModel):
    """A proposed page body that has no organizational authority before approval."""
    title: str = Field(min_length=2, max_length=200)
    page_type: str = Field(pattern="^(entity|concept|rule|procedure|decision)$")
    summary: str = Field(min_length=1, max_length=4_000)
    body: str = Field(min_length=1, max_length=100_000)
    tags: list[str] = Field(default_factory=list, max_length=100)
    valid_until: datetime | None = None
    supersedes_page_id: str | None = Field(default=None, max_length=160)


class CompileRequest(StrictModel):
    """Candidate input binding a conclusion and drafts to versioned compiler provenance."""
    root_task_id: str = Field(min_length=1, max_length=160)
    conclusion: str = Field(min_length=1, max_length=20_000)
    sources: list[KnowledgeSource] = Field(min_length=1, max_length=200)
    drafts: list[WikiDraft] = Field(min_length=1, max_length=50)
    compiler_model: str = Field(default="deterministic/template-v1", max_length=160)
    compiler_prompt_version: str = Field(default="wiki-compiler/v1", max_length=160)

    @model_validator(mode="after")
    def require_evidence(self):
        """Prevent inference-only pages from entering the human promotion workflow."""
        if not any(item.knowledge_level is KnowledgeLevel.RAW_EVIDENCE for item in self.sources):
            raise ValueError("at least one raw evidence source is required")
        return self


class WikiCandidate(StrictModel):
    """Persisted review unit retaining submitter, sources, compiler and mutable review state."""
    candidate_id: str = Field(default_factory=lambda: f"wkc_{uuid4().hex}")
    tenant_id: str
    submitted_by: str
    root_task_id: str
    conclusion: str
    sources: list[KnowledgeSource]
    drafts: list[WikiDraft]
    compiler_model: str
    compiler_prompt_version: str
    status: CandidateStatus = CandidateStatus.PENDING_REVIEW
    reviewer_id: str = ""
    review_comment: str = ""
    created_at: datetime = Field(default_factory=now)
    reviewed_at: datetime | None = None


class ReviewRequest(StrictModel):
    """Explicit decision plus expected state used for optimistic single consumption."""
    decision: str = Field(pattern="^(approve|reject)$")
    comment: str = Field(min_length=1, max_length=4_000)
    expected_status: CandidateStatus = CandidateStatus.PENDING_REVIEW


class WikiRelation(StrictModel):
    """Directed, explainable relationship between immutable Wiki page versions."""
    relation_type: RelationType
    target_page_id: str
    reason: str = Field(default="", max_length=1_000)


class WikiPage(StrictModel):
    """Immutable human-confirmed page version and its complete provenance graph."""
    page_id: str = Field(default_factory=lambda: f"wiki_{uuid4().hex}")
    tenant_id: str
    canonical_key: str
    title: str
    page_type: str
    summary: str
    body: str
    tags: list[str]
    knowledge_level: KnowledgeLevel = KnowledgeLevel.HUMAN_CONFIRMED
    sources: list[KnowledgeSource]
    relations: list[WikiRelation] = Field(default_factory=list)
    content_sha256: str
    version: int = Field(ge=1)
    valid_from: datetime = Field(default_factory=now)
    valid_until: datetime | None = None
    status: str = Field(default="active", pattern="^(active|expired|superseded)$")
    approved_by: str
    approval_comment: str
    candidate_id: str


class ReviewResult(StrictModel):
    """Return the consumed candidate, published pages and recoverable delivery identifiers."""
    candidate: WikiCandidate
    pages: list[WikiPage] = Field(default_factory=list)
    outbox_event_ids: list[str] = Field(default_factory=list)


class OutboxEvent(StrictModel):
    """Durable downstream intent with lease, bounded retry and DLQ state."""
    event_id: str = Field(default_factory=lambda: f"wke_{uuid4().hex}")
    tenant_id: str
    event_type: str
    payload: dict[str, Any]
    attempts: int = 0
    status: str = Field(default="pending", pattern="^(pending|processing|retry|delivered|dlq)$")
    next_attempt_at: datetime = Field(default_factory=now)
    lease_until: datetime | None = None
    lease_token: str = ""
    last_error: str = Field(default="", max_length=4_000)
    delivered_at: datetime | None = None
    created_at: datetime = Field(default_factory=now)
