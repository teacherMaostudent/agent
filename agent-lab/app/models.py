"""Agent Lab 的离线回放契约；所有实验输入均在创建时冻结。"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


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


class EvaluationBinding(StrictModel):
    """固定 Governance 评测资产，避免 Judge、Prompt 或量表在实验中漂移。"""

    rubric_id: str = Field(default="default", min_length=1, max_length=160)
    prompt_version_id: str | None = Field(default=None, max_length=160)
    retrieval_strategy_id: str | None = Field(default=None, max_length=160)
    calibration_run_id: str | None = Field(default=None, max_length=160)


class ExperimentPlan(StrictModel):
    """定义一次回放的不可变输入；只允许引用已发布 Agent，不接受草稿。"""

    name: str = Field(min_length=1, max_length=200)
    tenant_id: str = Field(min_length=1, max_length=160)
    agent_id: str = Field(min_length=2, max_length=160)
    environment: str = Field(default="laboratory", min_length=2, max_length=64)
    cases: list[ReplayCase] = Field(min_length=1, max_length=200)
    evaluation: EvaluationBinding = Field(default_factory=EvaluationBinding)
    baseline_experiment_id: str | None = Field(default=None, max_length=160)
    max_steps: int = Field(default=12, ge=2, le=30)
    deadline_seconds: int = Field(default=120, ge=1, le=600)
    max_cost_usd: float = Field(default=2.0, gt=0, le=10_000)


class SnapshotBinding(StrictModel):
    """记录用例被绑定到的发布快照，供回放和审计复现。"""

    case_id: str
    session_id: str
    release_id: str
    version_id: str
    snapshot_hash: str


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
    error: str | None = None


class ExperimentRecord(StrictModel):
    """聚合实验计划、冻结快照、回放结果及 Governance 评测引用。"""

    experiment_id: str
    plan: ExperimentPlan
    status: ExperimentStatus = ExperimentStatus.DRAFT
    snapshot_bindings: list[SnapshotBinding] = Field(default_factory=list)
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
