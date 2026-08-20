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


class SkillRunRequest(BaseModel):
    """执行 Active SkillVersion 的公开请求；完整计划始终由 Runtime 从 Control Plane 解析。"""

    skill_id: str = Field(min_length=2, max_length=160)
    version: str = Field(min_length=1, max_length=100)
    artifact_digest: str = Field(min_length=32, max_length=128)
    capability_id: str = Field(min_length=2, max_length=160)
    input: dict[str, Any] = Field(default_factory=dict)
    deadline_seconds: int = Field(default=120, ge=1, le=600)
    max_cost_usd: float = Field(default=2.0, gt=0, le=10_000)


class WorkflowRunRequest(BaseModel):
    """启动 Active WorkflowVersion；步骤和 Provider 目录只由 Control Plane 返回。"""

    workflow_id: str = Field(min_length=2, max_length=160)
    environment: str = Field(default="production", min_length=2, max_length=64)
    input: dict[str, Any] = Field(default_factory=dict)
    deadline_seconds: int = Field(default=300, ge=1, le=86_400)
    max_cost_usd: float = Field(default=5.0, gt=0, le=10_000)


class WorkflowResumeRequest(BaseModel):
    """以人工或外部信号恢复同一 Workflow 历史。"""

    signal: dict[str, Any] = Field(min_length=1)


class AgentResumeRequest(BaseModel):
    approved: bool
    approval_id: str = Field(default="", max_length=160)
    reason: str = Field(default="", max_length=2_000)
    selected_provider_agent_id: str = Field(default="", max_length=160)


class AgentRunInputRequest(BaseModel):
    """向活动 Run 注入下一安全边界生效的 Steering 或 Follow-up 消息。"""

    input_type: str = Field(pattern="^(steering|follow_up)$")
    message: str = Field(min_length=1, max_length=24_000)


class AgentFollowupRequest(BaseModel):
    """向已完成子 Agent 追加下一轮任务的受限请求，父运行必须通过谱系授权。"""

    task: str = Field(min_length=1, max_length=24_000)
    parent_run_id: str = Field(min_length=3, max_length=160)
    max_steps: int | None = Field(default=None, ge=2, le=30)
    max_cost_usd: float | None = Field(default=None, gt=0, le=10_000)


class SessionForkRequest(BaseModel):
    """从指定父会话事件前缀创建实验或子 Agent 会话，不复制原始消息正文。"""

    session_id: str = Field(min_length=3, max_length=160)
    seed_sequence: int | None = Field(default=None, ge=0)


class SessionCompactionRequest(BaseModel):
    """将旧模型 Surface 用受控摘要替换的请求；原 Event Ledger 不会被删除。"""

    replaced_through_sequence: int = Field(ge=1)
    summary: str = Field(min_length=1, max_length=12_000)
    policy_version: str = Field(min_length=1, max_length=160)
