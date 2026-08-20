"""顶层编排、Agent 推理、任务计划与状态分层的共享契约。

这些字段必须随发布工件冻结，不允许 Runtime 把 Workflow、Agent、
Skill 的状态或决策权重新混成一个通用 ``messages[]``。
"""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator


class ReasoningStrategy(StrEnum):
    """Agent 内部可用的推理策略，不代表顶层编排模式。"""

    MINIMAL = "minimal"
    PLAN = "plan"
    REACT = "react"
    REFLECT = "reflect"
    GRAPH = "graph"


class ReasoningPolicy(BaseModel):
    """发布时冻结的 Agent 推理边界。"""

    strategy: ReasoningStrategy = ReasoningStrategy.GRAPH
    max_replans: int = Field(default=2, ge=0, le=20)
    max_reflections: int = Field(default=1, ge=0, le=20)
    task_plan_required: bool = True


class CapabilityPolicy(BaseModel):
    """Planner 只能声明的能力集，Provider 选择由 Resolver 完成。"""

    required: list[str] = Field(default_factory=list, max_length=200)
    optional: list[str] = Field(default_factory=list, max_length=200)

    @model_validator(mode="after")
    def validate_unique_capabilities(self) -> "CapabilityPolicy":
        """拒绝重复或同时必需/可选的能力，避免降级语义不确定。"""
        required = [item.strip().upper() for item in self.required]
        optional = [item.strip().upper() for item in self.optional]
        if len(required) != len(set(required)) or len(optional) != len(set(optional)):
            raise ValueError("capability policy values must be unique")
        if set(required) & set(optional):
            raise ValueError("a capability cannot be both required and optional")
        self.required, self.optional = required, optional
        return self


class DurabilityPolicy(BaseModel):
    """RootTask 的持久性与恢复要求。"""

    durable: bool = False
    retry_policy_id: str = Field(default="runtime-default/v1", max_length=160)
    compensation_required: bool = False


class GovernancePolicy(BaseModel):
    """执行需遵循的治理版本，不把规则正文复制到 Runtime。"""

    policy_version: str = Field(
        default="governance-default/v1", min_length=1, max_length=160
    )
    qualification_required: bool = True
    audit_required: bool = True


class BudgetPolicy(BaseModel):
    """跨 Provider 共享的父任务资源上限。"""

    max_cost_usd: float = Field(default=2.0, gt=0, le=10_000)
    max_latency_ms: int = Field(default=300_000, ge=1, le=86_400_000)
    max_steps: int = Field(default=20, ge=1, le=1_000)


class VersionBindings(BaseModel):
    """定位一次计划依赖的不可变发布工件。"""

    snapshot_id: str = ""
    graph_version: str = ""
    model_policy_version: str = ""
    capability_catalog_version: str = ""


class TaskPlanStep(BaseModel):
    """Agent 在 ExecutionPlan 硬边界内生成的一项业务步骤。"""

    step_id: str = Field(min_length=1, max_length=160)
    objective: str = Field(min_length=1, max_length=2_000)
    capability_id: str = Field(default="", max_length=160)
    execution_strategy: Literal["DETERMINISTIC", "REACT"] = "DETERMINISTIC"
    verifier_id: str = Field(default="", max_length=160)
    status: str = Field(
        default="pending", pattern=r"^(pending|running|completed|failed|skipped)$"
    )


class TaskPlan(BaseModel):
    """Agent 的可修订解题计划；它不能改写 Owner、权限或发布策略。"""

    task_plan_id: str
    goal: str
    steps: list[TaskPlanStep] = Field(default_factory=list, max_length=100)
    revision: int = Field(default=1, ge=1)
    planning_strategy: Literal["PLAN_EXECUTE"] = "PLAN_EXECUTE"


class PlanAdmissionCheck(BaseModel):
    """计划进入执行阶段前的一项可审计确定性检查。"""

    check: str = Field(min_length=1, max_length=100)
    passed: bool
    reason: str = Field(default="", max_length=2_000)
    facts: dict[str, Any] = Field(default_factory=dict)


class PlanAdmissionDecision(BaseModel):
    """计划级准入结果；它绝不代表具体副作用已经获批。"""

    admission_id: str = Field(default_factory=lambda: f"admission_{uuid4().hex}")
    plan_id: str
    decision: Literal["ADMIT", "REJECT"]
    policy_version: str
    checks: list[PlanAdmissionCheck] = Field(min_length=1)
    allowed_tool_scope: list[str] = Field(default_factory=list)
    admitted_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @property
    def admitted(self) -> bool:
        """只有显式 ADMIT 且全部检查通过才允许进入执行引擎。"""
        return self.decision == "ADMIT" and all(item.passed for item in self.checks)


class ActionAuthorizationDecision(BaseModel):
    """绑定单个 operation/step 的动作级授权事实。"""

    operation_id: str = Field(min_length=1, max_length=200)
    step_id: str = Field(min_length=1, max_length=200)
    plan_id: str = Field(min_length=1, max_length=200)
    admission_id: str = Field(min_length=1, max_length=200)
    decision: Literal["ALLOW", "DENY", "REQUIRE_APPROVAL"]
    policy_version: str = Field(min_length=1, max_length=200)
    constraints: dict[str, Any] = Field(default_factory=dict)
    reason: str = Field(default="", max_length=2_000)


class RootTaskStatus(StrEnum):
    """跨 Workflow/Agent/Skill 的总任务状态。"""

    RUNNING = "RUNNING"
    WAITING = "WAITING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class WorkflowExecutionStatus(StrEnum):
    """Workflow 持久历史的控制状态。"""

    CREATED = "CREATED"
    RUNNING = "RUNNING"
    WAITING_SIGNAL = "WAITING_SIGNAL"
    COMPENSATING = "COMPENSATING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class SkillExecutionStatus(StrEnum):
    """Skill 的短生命周期状态，不扩展成长期状态机。"""

    CREATED = "CREATED"
    ACTIVATING = "ACTIVATING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
