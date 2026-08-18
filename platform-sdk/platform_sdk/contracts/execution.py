"""Runtime execution identity and durable state contracts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from pydantic import BaseModel, Field


class ExecutionContext(BaseModel):
    """Runtime-owned context propagated to every internal dependency."""

    request_id: str
    trace_id: str
    run_id: str
    parent_run_id: str = ""
    session_id: str
    parent_session_id: str = ""
    root_task_id: str = ""
    collaboration_snapshot_id: str = ""
    business_operation_id: str = ""
    tenant_id: str
    user_id: str
    agent_id: str
    agent_version: str
    snapshot_id: str
    graph_version: str = "runtime-planner-v1"
    model_policy_version: str = "local-unversioned"
    deadline_at: datetime
    attempt_budget_remaining: int = Field(ge=0, le=1000)

    @classmethod
    def create(
        cls,
        *,
        request_id: str,
        trace_id: str,
        session_id: str,
        tenant_id: str,
        user_id: str,
        agent_id: str,
        agent_version: str,
        snapshot_id: str,
        deadline_seconds: int,
        attempt_budget: int,
        graph_version: str = "runtime-planner-v1",
        model_policy_version: str = "local-unversioned",
        run_id: str | None = None,
        parent_run_id: str = "",
        parent_session_id: str = "",
        root_task_id: str = "",
        collaboration_snapshot_id: str = "",
        business_operation_id: str = "",
    ) -> ExecutionContext:
        """创建一次运行不可变的传播身份、截止时间和尝试预算。

        ``run_id`` 可由异步队列预先分配以保持重试关联；否则本地生成。deadline 使用
        UTC 绝对时间，避免跨服务因时区或相对超时产生不同判断。
        """
        # 先解析 Run ID，根任务默认值必须引用最终生成的 ID。直接在构造参数中使用
        # ``run_id or ...`` 会在未传入 run_id 时把 root_task_id 错误地留为空字符串。
        resolved_run_id = run_id or f"run_{uuid4().hex}"
        return cls(
            request_id=request_id,
            trace_id=trace_id,
            run_id=resolved_run_id,
            parent_run_id=parent_run_id,
            session_id=session_id,
            parent_session_id=parent_session_id,
            root_task_id=root_task_id or resolved_run_id,
            collaboration_snapshot_id=collaboration_snapshot_id,
            business_operation_id=business_operation_id,
            tenant_id=tenant_id,
            user_id=user_id,
            agent_id=agent_id,
            agent_version=agent_version,
            snapshot_id=snapshot_id,
            graph_version=graph_version,
            model_policy_version=model_policy_version,
            deadline_at=datetime.now(UTC) + timedelta(seconds=deadline_seconds),
            attempt_budget_remaining=attempt_budget,
        )

    def headers(self) -> dict[str, str]:
        """生成内部调用的审计/预算头；租户和用户不在此处伪造，由认证层提供。"""
        return {
            "X-Request-Id": self.request_id,
            "X-Trace-Id": self.trace_id,
            "X-Run-Id": self.run_id,
            "X-Parent-Run-Id": self.parent_run_id,
            "X-Session-Id": self.session_id,
            "X-Parent-Session-Id": self.parent_session_id,
            "X-Root-Task-Id": self.root_task_id,
            "X-Collaboration-Snapshot-Id": self.collaboration_snapshot_id,
            "X-Business-Operation-Id": self.business_operation_id,
            "X-Agent-Id": self.agent_id,
            "X-Agent-Version": self.agent_version,
            "X-Snapshot-Id": self.snapshot_id,
            "X-Graph-Version": self.graph_version,
            "X-Deadline-At": self.deadline_at.isoformat(),
            "X-Attempt-Budget-Remaining": str(self.attempt_budget_remaining),
        }

    def remaining_seconds(self) -> float:
        """计算距离绝对截止时间的非负秒数，供 HTTP 超时和降级决策使用。"""
        return max(0.0, (self.deadline_at - datetime.now(UTC)).total_seconds())


class RuntimeRun(BaseModel):
    """一次执行的持久化运行记录，状态机字段与对外结果状态分离。"""

    run_id: str
    tenant_id: str
    user_id: str
    agent_id: str
    snapshot_id: str
    status: str
    runtime_state: str = "CREATED"
    context: ExecutionContext
    result: dict = Field(default_factory=dict)
    error_code: str = ""
    cancel_requested: bool = False
    created_at: datetime
    updated_at: datetime
