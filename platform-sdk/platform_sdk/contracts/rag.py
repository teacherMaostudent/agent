from typing import Any

from pydantic import BaseModel, Field

from platform_sdk.contracts.models import Evidence


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


class RagIndexVersionResponse(BaseModel):
    """Immutable retrieval-index identity exposed to Runtime and release checks."""

    index_version: str
    backend: str
    api_version: str = "v1"


class RagCapabilitiesResponse(BaseModel):
    """Stable discovery contract; callers must not inspect RAG implementation details."""

    api_version: str = "v1"
    operations: list[str] = Field(
        default_factory=lambda: [
            "search",
            "controlled_scan",
            "ingestion",
            "index_version",
            "health",
        ]
    )


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
