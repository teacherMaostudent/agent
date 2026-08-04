from typing import Any

from pydantic import BaseModel, Field

from app.domain.models import Evidence


class RagSearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=4000)
    tenant_id: str = Field(default="default", max_length=160)
    user_id: str = Field(default="anonymous", max_length=160)
    document_id: str | None = Field(default=None, max_length=160)
    content: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    top_k: int = Field(default=8, ge=1, le=100)


class RagSearchResponse(BaseModel):
    query: str
    evidence: list[Evidence] = Field(default_factory=list)
    candidate_count: int = 0
    index_version: str = "local"


class ControlledScanRequest(BaseModel):
    scope: str = Field(min_length=1, max_length=80)
    pattern: str = Field(min_length=1, max_length=500)
    regex: bool = False
    glob: str = Field(default="**/*", max_length=160)


class ControlledScanMatch(BaseModel):
    scope: str
    path: str
    line_number: int
    line: str


class ControlledScanResponse(BaseModel):
    scope: str
    matches: list[ControlledScanMatch] = Field(default_factory=list)
