"""离线回放用例编排；不拥有 Runtime、Governance 或发布状态机。"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from uuid import uuid4

import httpx

from app.clients import ControlPlaneClient, GovernanceClient, RuntimeClient
from app.models import (
    CaseRun,
    ExperimentJob,
    ExperimentPlan,
    ExperimentRecord,
    ExperimentStatus,
    SnapshotBinding,
)
from app.repository import ExperimentRepositoryPort


class RetryableExperimentError(RuntimeError):
    """表示下游瞬态故障；Worker 应保留实验并通过租约任务进行受控重试。"""


class AgentLabService:
    """协调冻结快照、回放、治理评测和基线比较的唯一应用服务。"""

    def __init__(
        self,
        repository: ExperimentRepositoryPort,
        control_plane: ControlPlaneClient,
        runtime: RuntimeClient,
        governance: GovernanceClient,
        max_cases: int,
    ) -> None:
        """注入跨服务客户端，令测试可用假客户端验证编排边界。"""
        self._repository = repository
        self._control_plane = control_plane
        self._runtime = runtime
        self._governance = governance
        self._max_cases = max_cases

    def create(self, plan: ExperimentPlan) -> ExperimentRecord:
        """登记实验计划；重复用例标识会在执行前被拒绝以确保结果可对齐。"""
        if len(plan.cases) > self._max_cases:
            raise ValueError(f"case count exceeds AGENT_LAB_MAX_CASES={self._max_cases}")
        case_ids = [case.case_id for case in plan.cases]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("case_id must be unique within one experiment")
        record = ExperimentRecord(experiment_id=f"alx_{uuid4().hex}", plan=plan)
        return self._repository.create(record)

    def get(self, tenant_id: str, experiment_id: str) -> ExperimentRecord:
        """读取租户范围内实验；不存在时不泄漏其他租户是否拥有该标识。"""
        record = self._repository.get(tenant_id, experiment_id)
        if record is None:
            raise KeyError(experiment_id)
        return record

    def prepare(self, tenant_id: str, experiment_id: str) -> ExperimentRecord:
        """为每个用例预先解析 Session 绑定，并验证整个实验只使用一个快照版本。"""
        record = self.get(tenant_id, experiment_id)
        if record.status not in {ExperimentStatus.DRAFT, ExperimentStatus.PREPARED}:
            raise ValueError(f"cannot prepare experiment in {record.status}")
        bindings: list[SnapshotBinding] = []
        expected_identity: tuple[str, str, str] | None = None
        for case in record.plan.cases:
            session_id = f"lab-{record.experiment_id}-{case.case_id}"
            resolution = self._control_plane.resolve(
                tenant_id, record.plan.agent_id, record.plan.environment, session_id
            )
            snapshot = resolution.get("snapshot") or {}
            identity = (
                str(resolution.get("release_id", "")),
                str(resolution.get("version_id", "")),
                _snapshot_hash(snapshot),
            )
            if not all(identity):
                raise ValueError(
                    "Control Plane returned an incomplete published snapshot resolution"
                )
            if expected_identity is None:
                expected_identity = identity
            elif identity != expected_identity:
                raise ValueError(
                    "experiment cases resolved to different releases; "
                    "retry after rollout stabilizes"
                )
            bindings.append(
                SnapshotBinding(
                    case_id=case.case_id,
                    session_id=session_id,
                    release_id=identity[0],
                    version_id=identity[1],
                    snapshot_hash=identity[2],
                )
            )
        record.snapshot_bindings = bindings
        record.status = ExperimentStatus.PREPARED
        record.error = None
        record.updated_at = datetime.now(UTC)
        return self._repository.save(record)

    def run(self, tenant_id: str, experiment_id: str) -> ExperimentRecord:
        """顺序回放已冻结用例并将候选答案交给 Governance，失败结果仍会持久化。"""
        record = self.get(tenant_id, experiment_id)
        if record.status == ExperimentStatus.DRAFT:
            record = self.prepare(tenant_id, experiment_id)
        if record.status != ExperimentStatus.PREPARED:
            raise ValueError(f"cannot run experiment in {record.status}")
        bindings = {item.case_id: item for item in record.snapshot_bindings}
        if len(bindings) != len(record.plan.cases):
            raise ValueError("experiment has no complete frozen snapshot bindings")
        record.status = ExperimentStatus.RUNNING
        record.case_runs = []
        record.updated_at = datetime.now(UTC)
        self._repository.save(record)
        transport_failures: list[str] = []
        for case in record.plan.cases:
            binding = bindings[case.case_id]
            request_id = f"lab-run-{record.experiment_id}-{case.case_id}"
            try:
                result = self._runtime.run(
                    {
                        "task": case.task,
                        "agent_id": record.plan.agent_id,
                        "environment": record.plan.environment,
                        "document_id": case.document_id,
                        "content": case.content,
                        "metadata": {
                            **case.metadata,
                            "agent_lab_experiment_id": record.experiment_id,
                        },
                        "session_id": binding.session_id,
                        "max_steps": record.plan.max_steps,
                        "deadline_seconds": record.plan.deadline_seconds,
                        "max_cost_usd": record.plan.max_cost_usd,
                    },
                    tenant_id,
                    binding.session_id,
                    request_id,
                )
                record.case_runs.append(
                    CaseRun(
                        case_id=case.case_id,
                        session_id=binding.session_id,
                        run_id=result.get("run_id"),
                        status=str(result.get("status", "UNKNOWN")),
                        answer=str(result.get("answer", "")),
                        evidence_ids=_evidence_ids(result),
                        latency_ms=result.get("latency_ms"),
                        cost_usd=_cost(result),
                    )
                )
            except httpx.HTTPError as exc:
                transport_failures.append(f"{case.case_id}: {exc}")
                record.case_runs.append(
                    CaseRun(
                        case_id=case.case_id,
                        session_id=binding.session_id,
                        status="FAILED",
                        error=str(exc),
                    )
                )
            record.updated_at = datetime.now(UTC)
            self._repository.save(record)
        if transport_failures:
            record.status = ExperimentStatus.FAILED
            record.error = "runtime_transport_failed: " + "; ".join(transport_failures)[:3500]
            record.updated_at = datetime.now(UTC)
            self._repository.save(record)
            raise RetryableExperimentError(record.error)
        try:
            judge = self._governance.judge(tenant_id, _judge_request(record))
            record.judge_run_id = str(judge["id"])
            record.quality_gate = self._governance.quality_gate(
                tenant_id,
                record.judge_run_id,
                {"calibrationRunId": record.plan.evaluation.calibration_run_id},
            )
            record.status = ExperimentStatus.COMPLETED
            record.error = None
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            record.status = ExperimentStatus.FAILED
            record.error = f"governance_evaluation_failed: {exc}"
            record.updated_at = datetime.now(UTC)
            self._repository.save(record)
            raise RetryableExperimentError(record.error) from exc
        record.updated_at = datetime.now(UTC)
        return self._repository.save(record)

    def submit(self, tenant_id: str, experiment_id: str, *, max_attempts: int) -> ExperimentJob:
        """冻结实验后创建唯一持久化任务；API 只提交，不在 Web 进程执行长时回放。"""
        record = self.get(tenant_id, experiment_id)
        existing_job = self._repository.get_job_for_experiment(tenant_id, experiment_id)
        if existing_job is not None:
            if existing_job.status in {"COMPLETED", "DLQ"}:
                raise ValueError(f"cannot submit terminal experiment job in {existing_job.status}")
            return existing_job
        if record.status == ExperimentStatus.DRAFT:
            record = self.prepare(tenant_id, experiment_id)
        if record.status != ExperimentStatus.PREPARED:
            raise ValueError(f"cannot submit experiment in {record.status}")
        record.status = ExperimentStatus.QUEUED
        record.updated_at = datetime.now(UTC)
        self._repository.save(record)
        return self._repository.enqueue(
            ExperimentJob(
                job_id=f"alj_{uuid4().hex}",
                experiment_id=record.experiment_id,
                tenant_id=record.plan.tenant_id,
                max_attempts=max_attempts,
            )
        )

    def execute_claimed(self, job: ExperimentJob) -> ExperimentRecord:
        """由 Worker 对已领取任务执行回放；QUEUED 状态只允许转换为真正运行态。"""
        record = self.get(job.tenant_id, job.experiment_id)
        if record.status not in {ExperimentStatus.QUEUED, ExperimentStatus.FAILED}:
            raise ValueError(f"cannot execute experiment in {record.status}")
        record.status = ExperimentStatus.PREPARED
        record.updated_at = datetime.now(UTC)
        self._repository.save(record)
        return self.run(job.tenant_id, job.experiment_id)

    def mark_retry(self, job: ExperimentJob, error: str) -> ExperimentRecord:
        """将可重试失败重新标为排队，向查询方明确区分“正在恢复”和最终失败。"""
        record = self.get(job.tenant_id, job.experiment_id)
        record.status = ExperimentStatus.QUEUED
        record.error = f"retry_scheduled: {error[:3500]}"
        record.updated_at = datetime.now(UTC)
        return self._repository.save(record)

    def mark_dead_letter(self, job: ExperimentJob, error: str) -> ExperimentRecord:
        """将耗尽重试预算的实验收口为失败，避免查询端把 DLQ 中的任务误读为仍在运行。"""
        record = self.get(job.tenant_id, job.experiment_id)
        record.status = ExperimentStatus.FAILED
        record.error = f"dead_letter: {error[:3500]}"
        record.updated_at = datetime.now(UTC)
        return self._repository.save(record)

    def comparison(self, tenant_id: str, experiment_id: str) -> dict:
        """按用例成功率、成本和质量门禁比较当前实验与其声明的基线。"""
        current = self.get(tenant_id, experiment_id)
        baseline_id = current.plan.baseline_experiment_id
        if not baseline_id:
            raise ValueError("baseline_experiment_id is required for comparison")
        baseline = self.get(tenant_id, baseline_id)
        return {
            "experimentId": current.experiment_id,
            "baselineExperimentId": baseline.experiment_id,
            "snapshotChanged": _snapshot_identity(current) != _snapshot_identity(baseline),
            "current": _summary(current),
            "baseline": _summary(baseline),
            "qualityGateChanged": _gate_passed(current) != _gate_passed(baseline),
        }

    def release_evidence(self, tenant_id: str, experiment_id: str) -> dict:
        """返回可供 Control Plane 校验的实验事实，拒绝未完成或未通过门禁的记录。"""
        record = self.get(tenant_id, experiment_id)
        snapshot = _snapshot_identity(record)
        if record.status != ExperimentStatus.COMPLETED or not _gate_passed(record):
            raise ValueError("Agent Lab experiment is not completed with a passing quality gate")
        if not record.judge_run_id or not snapshot:
            raise ValueError("Agent Lab experiment lacks immutable snapshot or Judge evidence")
        return {
            "experimentId": record.experiment_id,
            "tenantId": record.plan.tenant_id,
            "agentId": record.plan.agent_id,
            "environment": record.plan.environment,
            "versionId": snapshot["version_id"],
            "releaseId": snapshot["release_id"],
            "snapshotHash": snapshot["snapshot_hash"],
            "judgeRunId": record.judge_run_id,
            "qualityGate": record.quality_gate,
        }


def _snapshot_hash(snapshot: dict) -> str:
    """计算规范化快照哈希，防止相同版本标识下的内容漂移被忽略。"""
    import json

    normalized = json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(normalized).hexdigest()


def _evidence_ids(result: dict) -> list[str]:
    """从 Runtime 结果提取证据标识，供 Governance 计算检索类指标。"""
    evidence = result.get("evidence") or result.get("knowledge_evidence") or []
    return [
        str(item.get("id") or item.get("chunk_id"))
        for item in evidence
        if isinstance(item, dict) and (item.get("id") or item.get("chunk_id"))
    ]


def _cost(result: dict) -> float | None:
    """读取统一 USD 成本字段；未知成本保持空值而不伪造零成本。"""
    value = result.get("cost_usd") or (result.get("usage") or {}).get("cost_usd")
    return float(value) if isinstance(value, int | float) else None


def _judge_request(record: ExperimentRecord) -> dict:
    """将已完成用例转换为 Governance Judge 的冻结候选答案和证据映射。"""
    runs = {item.case_id: item for item in record.case_runs}
    return {
        "rubricId": record.plan.evaluation.rubric_id,
        "promptVersionId": record.plan.evaluation.prompt_version_id,
        "retrievalStrategyId": record.plan.evaluation.retrieval_strategy_id,
        "caseIds": [case.case_id for case in record.plan.cases],
        "candidateAnswers": {
            case.task: runs[case.case_id].answer for case in record.plan.cases
        },
        "retrievedEvidenceByCase": {
            case.case_id: runs[case.case_id].evidence_ids for case in record.plan.cases
        },
        "metadata": {
            "agentLabExperimentId": record.experiment_id,
            "snapshot": _snapshot_identity(record),
        },
    }


def _snapshot_identity(record: ExperimentRecord) -> dict:
    """返回实验冻结的统一快照身份，供比较和审计输出复用。"""
    first = record.snapshot_bindings[0] if record.snapshot_bindings else None
    return {} if first is None else first.model_dump(exclude={"case_id", "session_id"})


def _summary(record: ExperimentRecord) -> dict:
    """汇总回放成功数、已知成本和治理门禁结论，避免基线比较依赖原始 Trace。"""
    costs = [item.cost_usd for item in record.case_runs if item.cost_usd is not None]
    return {
        "status": record.status,
        "succeededCases": sum(item.status == "SUCCEEDED" for item in record.case_runs),
        "totalCases": len(record.plan.cases),
        "totalKnownCostUsd": sum(costs),
        "qualityGatePassed": _gate_passed(record),
    }


def _gate_passed(record: ExperimentRecord) -> bool | None:
    """从 Governance 返回体读取唯一可信的门禁结论。"""
    return None if record.quality_gate is None else bool(record.quality_gate.get("passed"))
