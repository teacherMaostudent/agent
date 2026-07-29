from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from pydantic import BaseModel, Field


class ExecutionContext(BaseModel):
    """Runtime-owned context propagated to every internal dependency."""

    request_id: str
    trace_id: str
    run_id: str
    session_id: str
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
    ) -> ExecutionContext:
        return cls(
            request_id=request_id,
            trace_id=trace_id,
            run_id=f"run_{uuid4().hex}",
            session_id=session_id,
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
        return {
            "X-Request-Id": self.request_id,
            "X-Trace-Id": self.trace_id,
            "X-Run-Id": self.run_id,
            "X-Session-Id": self.session_id,
            "X-Agent-Id": self.agent_id,
            "X-Agent-Version": self.agent_version,
            "X-Snapshot-Id": self.snapshot_id,
            "X-Graph-Version": self.graph_version,
            "X-Deadline-At": self.deadline_at.isoformat(),
            "X-Attempt-Budget-Remaining": str(self.attempt_budget_remaining),
        }

    def remaining_seconds(self) -> float:
        return max(0.0, (self.deadline_at - datetime.now(UTC)).total_seconds())


class RuntimeRun(BaseModel):
    run_id: str
    tenant_id: str
    user_id: str
    agent_id: str
    snapshot_id: str
    status: str
    context: ExecutionContext
    result: dict = Field(default_factory=dict)
    error_code: str = ""
    cancel_requested: bool = False
    created_at: datetime
    updated_at: datetime
