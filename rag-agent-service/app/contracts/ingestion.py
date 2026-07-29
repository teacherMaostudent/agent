from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field

from app.domain.models import utc_now


class JobStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class JobCreateRequest(BaseModel):
    job_type: str = Field(pattern="^(PARSE|OCR|REINDEX)$")
    document_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class IngestionJob(BaseModel):
    job_id: str = Field(default_factory=lambda: f"job_{uuid4().hex[:16]}")
    job_type: str
    document_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    status: JobStatus = JobStatus.QUEUED
    attempts: int = 0
    result: dict[str, Any] = Field(default_factory=dict)
    error: str = ""
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
