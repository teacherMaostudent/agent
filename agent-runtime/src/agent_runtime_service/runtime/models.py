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


class ExecutionMode(StrEnum):
    """旧版单轴执行模式投影，仅用于兼容已发布的 Profile。"""

    FAST = "fast"
    AGENTIC = "agentic"
    GRAPH = "graph"
    DURABLE = "durable"


class ExecutionLifecycle(StrEnum):
    """一次 Run 的耐久性边界；它不决定模型是否进行多步推理。"""

    REQUEST_SCOPED = "request_scoped"
    DURABLE_WORKFLOW = "durable_workflow"


class ReasoningMode(StrEnum):
    """执行器内部允许的推理强度；它不决定是否交由 Temporal 调度。"""

    MINIMAL = "minimal"
    AGENTIC = "agentic"
    GRAPH = "graph"


class ExecutionRequirements(BaseModel):
    """发布快照声明的双轴执行需求。

    生命周期和推理模式故意拆开：同一个受控 Graph 可以同步短执行，也可以由
    Durable Workflow 可靠驱动。Profile 只是 Runtime 集群为这一组合提供的部署别名。
    """

    lifecycle: ExecutionLifecycle = ExecutionLifecycle.REQUEST_SCOPED
    reasoning: ReasoningMode = ReasoningMode.GRAPH


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
    executor_profile: str
    execution_mode: ExecutionMode = ExecutionMode.GRAPH
    execution_requirements: ExecutionRequirements = Field(default_factory=ExecutionRequirements)
    intent_catalog_version: str = "platform-default/v1"
    retrieval_policy: dict[str, Any] = Field(default_factory=dict)
    planner_version: str
    analyzer_version: str
    input_fingerprint: str
    policy_fingerprint: str
    plan_hash: str
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


class UserInputResume(BaseModel):
    """恢复澄清中断的邮箱租约引用；用户正文仍只由 Context 服务保存。"""

    message_id: str
    lease_token: str


class RuntimeLimitExceeded(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        """携带稳定机器码的预期限制异常，API 不必解析自然语言错误消息。"""
        super().__init__(message)
        self.code = code


class RuntimeCancelled(RuntimeError):
    pass
