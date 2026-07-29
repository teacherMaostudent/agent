from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.domain.models import Evidence, utc_now


class ConversationMessage(BaseModel):
    role: str = Field(pattern="^(user|assistant|system|tool)$")
    content: str = Field(min_length=1)
    created_at: datetime = Field(default_factory=utc_now)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ContextAssembleRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=160)
    query: str = Field(min_length=1, max_length=4000)
    tenant_id: str = Field(default="default", max_length=160)
    user_id: str = Field(default="anonymous", max_length=160)
    document_id: str | None = Field(default=None, max_length=160)
    content: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    top_k: int = Field(default=8, ge=1, le=100)
    token_budget: int | None = Field(default=None, ge=512, le=200000)


class ContextPackage(BaseModel):
    session_id: str
    recent_messages: list[ConversationMessage] = Field(default_factory=list)
    knowledge_evidence: list[Evidence] = Field(default_factory=list)
    user_context: dict[str, Any] = Field(default_factory=dict)
    token_budget: int
    estimated_tokens: int
    truncated: bool = False
