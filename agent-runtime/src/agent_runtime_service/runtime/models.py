from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class RouteType(StrEnum):
    CLARIFY = "clarify"
    DIRECT = "direct"
    RAG = "rag"
    TOOL = "tool"
    DEEP_REASONING = "deep_reasoning"


class IntentResult(BaseModel):
    name: str
    confidence: float = Field(ge=0, le=1)
    reason: str = ""


class EntityResult(BaseModel):
    name: str
    value: Any
    confidence: float = Field(default=1.0, ge=0, le=1)


class SourcePlan(BaseModel):
    knowledge_bases: list[str] = Field(default_factory=list)
    context_sources: list[str] = Field(default_factory=list)
    required_permissions: list[str] = Field(default_factory=list)
    reason: str = ""


class ComplexityAssessment(BaseModel):
    score: int = Field(ge=0, le=100)
    level: str
    reasons: list[str] = Field(default_factory=list)


class SlaAssessment(BaseModel):
    deadline_at: datetime
    remaining_ms: int = Field(ge=0)
    tier: str
    feasible: bool


class CostAssessment(BaseModel):
    max_cost_usd: float = Field(gt=0)
    estimated_cost_usd: float = Field(ge=0)
    remaining_cost_usd: float = Field(ge=0)
    feasible: bool


class RouteDecision(BaseModel):
    route: RouteType
    quality_tier: str
    reasons: list[str] = Field(default_factory=list)
    fallback_chain: list[RouteType] = Field(default_factory=list)


class ExecutionPlan(BaseModel):
    plan_id: str
    intent: IntentResult
    entities: list[EntityResult]
    source_plan: SourcePlan
    complexity: ComplexityAssessment
    sla: SlaAssessment
    cost: CostAssessment
    route: RouteDecision
    agent_version: str
    graph_version: str
    model_policy_version: str
    retrieval_policy: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class RuntimeBudget(BaseModel):
    deadline_at: datetime
    max_steps: int = Field(ge=1)
    max_llm_calls: int = Field(ge=0)
    max_tool_calls: int = Field(ge=0)
    max_retrieval_rounds: int = Field(ge=0)
    max_cost_usd: float = Field(gt=0)
    max_attempts: int = Field(default=100, ge=0)
    step_count: int = Field(default=0, ge=0)
    llm_calls: int = Field(default=0, ge=0)
    tool_calls: int = Field(default=0, ge=0)
    retrieval_rounds: int = Field(default=0, ge=0)
    spent_cost_usd: float = Field(default=0, ge=0)
    attempts_used: int = Field(default=0, ge=0)

    @property
    def remaining_cost_usd(self) -> float:
        """返回非负剩余成本，避免下游因浮点微小误差得到负额度。"""
        return max(0.0, self.max_cost_usd - self.spent_cost_usd)

    @property
    def remaining_ms(self) -> int:
        """以 UTC 绝对截止时间计算剩余毫秒，供 SLA 与超时守卫共享。"""
        return max(0, int((self.deadline_at - datetime.now(UTC)).total_seconds() * 1_000))

    @property
    def remaining_attempts(self) -> int:
        """返回未使用下游尝试次数，不能小于零。"""
        return max(0, self.max_attempts - self.attempts_used)


class ApprovalResume(BaseModel):
    approved: bool
    approval_id: str = ""
    decided_by: str = ""
    reason: str = ""


class RuntimeLimitExceeded(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        """携带稳定机器码的预期限制异常，API 不必解析自然语言错误消息。"""
        super().__init__(message)
        self.code = code


class RuntimeCancelled(RuntimeError):
    pass
