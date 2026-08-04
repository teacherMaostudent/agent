from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from hashlib import sha256
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex}"


class LiteDocument(BaseModel):
    document_id: str = Field(default_factory=lambda: new_id("doc"))
    filename: str
    document_type: str = "gmp_document"
    version: str = "1"
    text: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    content_hash: str = ""
    created_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def calculate_hash(self) -> "LiteDocument":
        if not self.content_hash:
            payload = f"{self.filename}\n{self.version}\n{self.text}".encode("utf-8")
            self.content_hash = sha256(payload).hexdigest()
        return self


class RegulationClause(BaseModel):
    clause_id: str = Field(default_factory=lambda: new_id("clause"))
    regulation_id: str
    regulation_version: str
    title: str
    text: str
    applicable_document_types: list[str] = Field(default_factory=list)
    # Every concept must match at least one synonym. Example:
    # {"deviation_record": ["偏差记录", "偏差报告"]}
    required_concepts: dict[str, list[str]] = Field(default_factory=dict)
    forbidden_terms: list[str] = Field(default_factory=list)
    absence_is_failure: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)


class ConsistencyRule(BaseModel):
    rule_id: str
    title: str
    aliases: list[str]
    # Must contain a named group called "value", or the full match is used.
    value_pattern: str
    severity: str = "MEDIUM"


class ExternalReviewRequest(BaseModel):
    document_ids: list[str] = Field(default_factory=list)
    clause_ids: list[str] = Field(default_factory=list)
    allow_llm: bool = False


class InternalReviewRequest(BaseModel):
    document_ids: list[str] = Field(default_factory=list)
    rules: list[ConsistencyRule]
    allow_llm: bool = False


class DecisionStatus(StrEnum):
    RULE_PASS = "RULE_PASS"
    RULE_FAIL = "RULE_FAIL"
    UNCERTAIN = "UNCERTAIN"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class ReviewFinding(BaseModel):
    finding_id: str = Field(default_factory=lambda: new_id("finding"))
    status: DecisionStatus
    finding_type: str
    severity: str = "MEDIUM"
    document_ids: list[str] = Field(default_factory=list)
    clause_id: str | None = None
    rule_id: str | None = None
    reason: str
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    llm_required: bool = False


class ReviewMetrics(BaseModel):
    documents_considered: int = 0
    total_comparisons: int = 0
    not_applicable: int = 0
    rule_pass: int = 0
    rule_fail: int = 0
    uncertain: int = 0
    llm_calls: int = 0
    rule_resolution_rate: float = 0.0
    llm_candidate_rate: float = 0.0

    def finalize(self) -> None:
        decisions = self.rule_pass + self.rule_fail + self.uncertain
        if decisions:
            self.rule_resolution_rate = round(
                (self.rule_pass + self.rule_fail) / decisions, 6
            )
            self.llm_candidate_rate = round(self.uncertain / decisions, 6)


class ReviewJob(BaseModel):
    job_id: str = Field(default_factory=lambda: new_id("review"))
    review_type: str
    status: str = "COMPLETED"
    created_at: datetime = Field(default_factory=utc_now)
    completed_at: datetime = Field(default_factory=utc_now)
    metrics: ReviewMetrics
    findings: list[ReviewFinding] = Field(default_factory=list)


class FeedbackInput(BaseModel):
    finding_id: str
    decision: str
    note: str = ""
    reviewer: str = "anonymous"


class HistoryEvent(BaseModel):
    event_id: str = Field(default_factory=lambda: new_id("event"))
    event_type: str
    object_id: str
    actor: str = "system"
    payload: dict[str, Any] = Field(default_factory=dict)
    occurred_at: datetime = Field(default_factory=utc_now)
