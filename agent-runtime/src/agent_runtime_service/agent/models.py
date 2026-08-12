from enum import StrEnum
from typing import Any, TypedDict

from pydantic import BaseModel, Field, model_validator


class AgentAction(StrEnum):
    RETRIEVE = "RETRIEVE"
    TOOL = "TOOL"
    ANSWER = "ANSWER"


class AgentDecision(BaseModel):
    action: AgentAction
    reason: str = ""
    query: str = ""
    tool_name: str = ""
    tool_arguments: dict[str, Any] = Field(default_factory=dict)
    final_answer: str = ""

    @model_validator(mode="after")
    def validate_action_payload(self) -> "AgentDecision":
        """确保模型每次只给出一个可执行动作所必需的字段。

        该校验是模型输出进入图路由前的第一层；工具名称和最终回答仍会被发布快照、
        工具目录及输出 Schema 继续约束。
        """
        if self.action == AgentAction.RETRIEVE and not self.query.strip():
            raise ValueError("RETRIEVE requires query")
        if self.action == AgentAction.TOOL and not self.tool_name.strip():
            raise ValueError("TOOL requires tool_name")
        if self.action == AgentAction.ANSWER and not self.final_answer.strip():
            raise ValueError("ANSWER requires final_answer")
        return self


class AgentState(TypedDict, total=False):
    task: str
    document_id: str | None
    content: str | None
    metadata: dict[str, Any]
    tenant_id: str
    user_id: str
    permissions: list[str]
    request_id: str
    session_id: str
    trace_id: str
    run_id: str
    agent_id: str
    agent_version: str
    snapshot_id: str
    agent_snapshot: dict[str, Any]
    compiled_plan: dict[str, Any]
    executor_profile: str
    graph_version: str
    flow_version: int
    deadline_at: str
    attempt_budget_remaining: int
    budget: dict[str, Any]
    intent: dict[str, Any]
    entities: list[dict[str, Any]]
    source_plan: dict[str, Any]
    execution_plan: dict[str, Any]
    workflow_cursor: str
    execution_trace: list[dict[str, Any]]
    pending_approval: dict[str, Any]
    step_count: int
    max_steps: int
    observations: list[dict[str, Any]]
    evidence: list[dict[str, Any]]
    conversation_history: list[dict[str, Any]]
    user_context: dict[str, Any]
    context_status: dict[str, Any]
    decision: dict[str, Any]
    final_answer: str
    termination_reason: str
    safety_status: str


class AgentRunResult(BaseModel):
    status: str
    answer: str
    steps: int
    termination_reason: str
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    observations: list[dict[str, Any]] = Field(default_factory=list)
    execution_plan: dict[str, Any] = Field(default_factory=dict)
    budget: dict[str, Any] = Field(default_factory=dict)
    execution_trace: list[dict[str, Any]] = Field(default_factory=list)
    interrupts: list[dict[str, Any]] = Field(default_factory=list)
