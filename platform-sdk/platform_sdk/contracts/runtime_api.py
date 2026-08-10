"""Public Runtime request contracts, independent of any RAG application package."""

from typing import Any

from pydantic import BaseModel, Field


class AgentRunRequest(BaseModel):
    task: str
    agent_id: str = Field(default="general-agent", min_length=2, max_length=160)
    environment: str = Field(default="production", min_length=2, max_length=64)
    document_id: str | None = None
    content: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    session_id: str | None = Field(default=None, max_length=160)
    max_steps: int | None = Field(default=None, ge=2, le=30)
    deadline_seconds: int | None = Field(default=None, ge=1, le=600)
    attempt_budget: int | None = Field(default=None, ge=0, le=100)
    max_cost_usd: float | None = Field(default=None, gt=0, le=10_000)


class AgentResumeRequest(BaseModel):
    approved: bool
    approval_id: str = Field(default="", max_length=160)
    reason: str = Field(default="", max_length=2_000)
