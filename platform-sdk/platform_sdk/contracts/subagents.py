"""发布快照声明的受控子 Agent 委派契约。"""

from pydantic import BaseModel, Field


class SubAgentBinding(BaseModel):
    """父 Agent 可委派的目标、递归深度及资源上限；未声明目标不得调用。"""

    agent_id: str = Field(min_length=2, max_length=160)
    max_depth: int = Field(default=1, ge=1, le=4)
    max_budget_fraction: float = Field(default=0.25, gt=0, le=1)
    max_invocations: int = Field(default=1, ge=1, le=10)
