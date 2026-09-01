"""Agent Lab 的离线回放契约；所有实验输入均在创建时冻结。"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


def utc_now() -> datetime:
    """返回统一 UTC 时间，保证跨服务实验记录可排序和可追溯。"""
    return datetime.now(UTC)


class StrictModel(BaseModel):
    """拒绝未登记字段，防止回放输入在 API 边界发生语义漂移。"""

    model_config = ConfigDict(extra="forbid")


class ExperimentStatus(StrEnum):
    """定义实验可达状态，禁止 API 用任意字符串伪造运行结论。"""

    DRAFT = "DRAFT"
    PREPARED = "PREPARED"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class ExperimentJobStatus(StrEnum):
    """定义持久化回放任务的队列状态，实验结论和调度状态不得混为一谈。"""

    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    RETRY_SCHEDULED = "RETRY_SCHEDULED"
    COMPLETED = "COMPLETED"
    DLQ = "DLQ"


class ReplayCase(StrictModel):
    """定义一条可重复执行的 Agent 输入及其可选证据预期。"""

    case_id: str = Field(min_length=1, max_length=160)
    task: str = Field(min_length=1, max_length=20_000)
    document_id: str | None = Field(default=None, max_length=160)
    content: str | None = Field(default=None, max_length=100_000)
    metadata: dict[str, Any] = Field(default_factory=dict)
    expected_evidence_ids: list[str] = Field(default_factory=list)
    expected_tool_names: list[str] = Field(default_factory=list)


class CaseTrajectoryMetrics(StrictModel):
    """从脱敏 Session Ledger 派生的 Harness 行为指标，不依赖 Judge 主观评分。"""

    task_succeeded: bool = False
    tool_call_count: int = Field(default=0, ge=0)
    model_call_count: int = Field(default=0, ge=0)
    retrieval_round_count: int = Field(default=0, ge=0)
    approval_count: int = Field(default=0, ge=0)
    recovery_event_count: int = Field(default=0, ge=0)
    permission_violation_count: int = Field(default=0, ge=0)
    evidence_recall: float | None = Field(default=None, ge=0, le=1)
    tool_selection_precision: float | None = Field(default=None, ge=0, le=1)


class EvaluationBinding(StrictModel):
    """固定 Governance 评测资产，避免 Judge、Prompt 或量表在实验中漂移。"""

    rubric_id: str = Field(default="default", min_length=1, max_length=160)
    prompt_version_id: str | None = Field(default=None, max_length=160)
    retrieval_strategy_id: str | None = Field(default=None, max_length=160)
    calibration_run_id: str | None = Field(default=None, max_length=160)


class SandboxCase(StrictModel):
    """声明一项在隔离环境验证的工具或代码用例，而不是把任意 Shell 字符串交给 Worker。"""

    case_id: str = Field(min_length=1, max_length=160)
    image: str = Field(min_length=1, max_length=300)
    command: list[str] = Field(min_length=1, max_length=40)
    timeout_seconds: int = Field(default=60, ge=1, le=600)
    expected_exit_code: int = Field(default=0, ge=0, le=255)

    @model_validator(mode="after")
    def validate_argv(self) -> SandboxCase:
        """拒绝换行和空参数，确保 Provider 永远以 argv 而非 shell 解释命令。"""
        if any(not item.strip() or "\n" in item or "\r" in item for item in self.command):
            raise ValueError("sandbox command must contain non-empty single-line argv entries")
        return self


class ExperimentPlan(StrictModel):
    """定义 Agent 或 Skill 回放；两类目标都只能引用已发布工件。"""

    name: str = Field(min_length=1, max_length=200)
    tenant_id: str = Field(min_length=1, max_length=160)
    target_type: Literal["agent", "skill"] = "agent"
    agent_id: str = Field(default="", max_length=160)
    skill_id: str = Field(default="", max_length=160)
    skill_version: str = Field(default="", max_length=100)
    skill_capability_id: str = Field(default="", max_length=160)
    environment: str = Field(default="laboratory", min_length=2, max_length=64)
    cases: list[ReplayCase] = Field(min_length=1, max_length=200)
    sandbox_cases: list[SandboxCase] = Field(default_factory=list, max_length=50)
    evaluation: EvaluationBinding = Field(default_factory=EvaluationBinding)
    baseline_experiment_id: str | None = Field(default=None, max_length=160)
    max_steps: int = Field(default=12, ge=2, le=30)
    deadline_seconds: int = Field(default=120, ge=1, le=600)
    max_cost_usd: float = Field(default=2.0, gt=0, le=10_000)

    @model_validator(mode="after")
    def validate_target(self) -> ExperimentPlan:
        """要求实验只选择一种目标，并为 Skill 固定版本和能力。"""
        if self.target_type == "agent" and not self.agent_id.strip():
            raise ValueError("agent experiment requires agent_id")
        if self.target_type == "skill" and not all(
            (self.skill_id.strip(), self.skill_version.strip(), self.skill_capability_id.strip())
        ):
            raise ValueError("skill experiment requires skill_id, skill_version and capability_id")
        return self


class SnapshotBinding(StrictModel):
    """记录用例被绑定到的发布快照，供回放和审计复现。"""

    case_id: str
    session_id: str
    release_id: str
    version_id: str
    snapshot_hash: str
    target_type: Literal["agent", "skill"] = "agent"


class CaseRun(StrictModel):
    """保存一个回放用例的运行结果或可解释失败信息。"""

    case_id: str
    session_id: str
    run_id: str | None = None
    status: str
    answer: str = ""
    evidence_ids: list[str] = Field(default_factory=list)
    latency_ms: int | None = None
    cost_usd: float | None = None
    session_event_count: int | None = Field(default=None, ge=0)
    session_last_sequence: int | None = Field(default=None, ge=0)
    trajectory: CaseTrajectoryMetrics = Field(default_factory=CaseTrajectoryMetrics)
    error: str | None = None


class SandboxCaseRun(StrictModel):
    """保存隔离验证的可审计摘要；默认不把可能敏感的标准输出写进实验数据库。"""

    case_id: str
    image: str
    command_sha256: str
    provider: str
    status: Literal["PASSED", "FAILED", "ERROR"]
    exit_code: int | None = None
    expected_exit_code: int
    stdout_sha256: str | None = None
    stderr_sha256: str | None = None
    stdout_bytes: int = Field(default=0, ge=0)
    stderr_bytes: int = Field(default=0, ge=0)
    error: str | None = None


class ExperimentRecord(StrictModel):
    """聚合实验计划、冻结快照、回放结果及 Governance 评测引用。"""

    experiment_id: str
    plan: ExperimentPlan
    status: ExperimentStatus = ExperimentStatus.DRAFT
    snapshot_bindings: list[SnapshotBinding] = Field(default_factory=list)
    sandbox_runs: list[SandboxCaseRun] = Field(default_factory=list)
    case_runs: list[CaseRun] = Field(default_factory=list)
    judge_run_id: str | None = None
    quality_gate: dict[str, Any] | None = None
    error: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class ExperimentJob(StrictModel):
    """表示可被独立 Worker 领取的一次回放尝试及其持久化租约。"""

    job_id: str
    experiment_id: str
    tenant_id: str
    status: ExperimentJobStatus = ExperimentJobStatus.QUEUED
    attempt_count: int = Field(default=0, ge=0)
    max_attempts: int = Field(default=5, ge=1, le=20)
    available_at: datetime = Field(default_factory=utc_now)
    leased_by: str | None = None
    lease_expires_at: datetime | None = None
    last_error: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
