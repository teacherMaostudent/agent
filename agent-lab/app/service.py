"""离线回放用例编排；不拥有 Runtime、Governance 或发布状态机。"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from uuid import uuid4

import httpx

from app.clients import ControlPlaneClient, GovernanceClient, RuntimeClient
from app.models import (
    CaseRun,
    CaseTrajectoryMetrics,
    ExperimentJob,
    ExperimentPlan,
    ExperimentRecord,
    ExperimentStatus,
    SandboxCaseRun,
    SnapshotBinding,
)
from app.repository import ExperimentRepositoryPort
from app.sandbox import SandboxProvider, SandboxRequest


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
        *,
        sandbox: SandboxProvider | None = None,
        sandbox_image_allowlist: set[str] | None = None,
    ) -> None:
        """注入跨服务客户端，令测试可用假客户端验证编排边界。"""
        self._repository = repository
        self._control_plane = control_plane
        self._runtime = runtime
        self._governance = governance
        self._max_cases = max_cases
        self._sandbox = sandbox
        self._sandbox_image_allowlist = sandbox_image_allowlist or set()

    def create(self, plan: ExperimentPlan) -> ExperimentRecord:
        """登记实验计划；重复用例标识会在执行前被拒绝以确保结果可对齐。"""
        if len(plan.cases) > self._max_cases:
            raise ValueError(f"case count exceeds AGENT_LAB_MAX_CASES={self._max_cases}")
        case_ids = [case.case_id for case in plan.cases]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("case_id must be unique within one experiment")
        sandbox_case_ids = [case.case_id for case in plan.sandbox_cases]
        if len(sandbox_case_ids) != len(set(sandbox_case_ids)):
            raise ValueError("sandbox case_id must be unique within one experiment")
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
            if record.plan.target_type == "skill":
                resolution = self._control_plane.resolve_skill(
                    tenant_id, record.plan.skill_id, record.plan.skill_version
                )
                identity = (
                    f"skill:{record.plan.skill_id}",
                    str(resolution.get("version", "")),
                    str(resolution.get("artifact_digest", "")),
                )
            else:
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
                    target_type=record.plan.target_type,
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
        record.sandbox_runs = []
        record.updated_at = datetime.now(UTC)
        self._repository.save(record)
        if not self._run_sandbox_cases(record):
            # Sandbox assertions are deterministic experiment evidence. They are not a Runtime
            # transport problem and therefore must not be retried by the regular replay queue.
            record.status = ExperimentStatus.FAILED
            record.error = "sandbox_validation_failed"
            record.updated_at = datetime.now(UTC)
            return self._repository.save(record)
        transport_failures: list[str] = []
        for case in record.plan.cases:
            binding = bindings[case.case_id]
            request_id = f"lab-run-{record.experiment_id}-{case.case_id}"
            try:
                if record.plan.target_type == "skill":
                    result = self._runtime.run_skill(
                        {
                            "skill_id": record.plan.skill_id,
                            "version": record.plan.skill_version,
                            "artifact_digest": binding.snapshot_hash,
                            "capability_id": record.plan.skill_capability_id,
                            "input": {
                                "task": case.task,
                                "document_id": case.document_id,
                                "content": case.content,
                                "metadata": case.metadata,
                            },
                            "max_cost_usd": record.plan.max_cost_usd,
                            "deadline_seconds": record.plan.deadline_seconds,
                        },
                        tenant_id,
                        request_id,
                    )
                else:
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
                try:
                    ledger = (
                        self._runtime.session_events(tenant_id, binding.session_id)
                        if hasattr(self._runtime, "session_events")
                        else {}
                    )
                except httpx.HTTPError:
                    # 回放结论已由 Runtime 返回; Ledger 读取失败应标记为缺少解释材料,
                    # 不能把一次已完成的线上执行改写成失败实验。
                    ledger = {}
                session_events = ledger.get("events", []) if isinstance(ledger, dict) else []
                record.case_runs.append(
                    CaseRun(
                        case_id=case.case_id,
                        session_id=binding.session_id,
                        run_id=result.get("run_id"),
                        status=str(result.get("status", "UNKNOWN")),
                        answer=_candidate_answer(result, record.plan.target_type),
                        evidence_ids=_evidence_ids(result),
                        latency_ms=result.get("latency_ms"),
                        cost_usd=_cost(result),
                        session_event_count=len(session_events),
                        session_last_sequence=(
                            int(ledger.get("next_after_sequence", 0))
                            if isinstance(ledger, dict)
                            else None
                        ),
                        trajectory=_trajectory_metrics(
                            session_events,
                            expected_evidence_ids=case.expected_evidence_ids,
                            expected_tool_names=case.expected_tool_names,
                            actual_evidence_ids=_evidence_ids(result),
                            status=str(result.get("status", "UNKNOWN")),
                        ),
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

    def _run_sandbox_cases(self, record: ExperimentRecord) -> bool:
        """执行计划声明的隔离验证，并仅持久化输出摘要以避免实验库收集原始秘密。"""
        if not record.plan.sandbox_cases:
            return True
        if self._sandbox is None:
            raise ValueError("sandbox cases require an Agent Lab sandbox provider")
        passed = True
        for case in record.plan.sandbox_cases:
            if case.image not in self._sandbox_image_allowlist:
                record.sandbox_runs.append(
                    SandboxCaseRun(
                        case_id=case.case_id,
                        image=case.image,
                        command_sha256=_sha256_json(case.command),
                        provider="unavailable",
                        status="ERROR",
                        expected_exit_code=case.expected_exit_code,
                        error="sandbox_image_not_allowlisted",
                    )
                )
                passed = False
                continue
            try:
                result = self._sandbox.execute(
                    SandboxRequest(
                        image=case.image,
                        command=tuple(case.command),
                        timeout_seconds=case.timeout_seconds,
                        network="none",
                    )
                )
                status = "PASSED" if result.exit_code == case.expected_exit_code else "FAILED"
                record.sandbox_runs.append(
                    SandboxCaseRun(
                        case_id=case.case_id,
                        image=case.image,
                        command_sha256=_sha256_json(case.command),
                        provider=result.provider,
                        status=status,
                        exit_code=result.exit_code,
                        expected_exit_code=case.expected_exit_code,
                        stdout_sha256=_sha256_text(result.stdout),
                        stderr_sha256=_sha256_text(result.stderr),
                        stdout_bytes=len(result.stdout.encode("utf-8")),
                        stderr_bytes=len(result.stderr.encode("utf-8")),
                    )
                )
                passed = passed and status == "PASSED"
            except (OSError, RuntimeError, TimeoutError) as exc:
                record.sandbox_runs.append(
                    SandboxCaseRun(
                        case_id=case.case_id,
                        image=case.image,
                        command_sha256=_sha256_json(case.command),
                        provider="unavailable",
                        status="ERROR",
                        expected_exit_code=case.expected_exit_code,
                        error=f"sandbox_execution_error: {str(exc)[:500]}",
                    )
                )
                passed = False
            record.updated_at = datetime.now(UTC)
            self._repository.save(record)
        return passed

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
        evidence = {
            "experimentId": record.experiment_id,
            "tenantId": record.plan.tenant_id,
            "environment": record.plan.environment,
            "versionId": snapshot["version_id"],
            "releaseId": snapshot["release_id"],
            "snapshotHash": snapshot["snapshot_hash"],
            "judgeRunId": record.judge_run_id,
            "qualityGate": record.quality_gate,
        }
        if record.plan.target_type == "skill":
            evidence.update(
                {
                    "targetType": "skill",
                    "skillId": record.plan.skill_id,
                }
            )
        else:
            evidence["agentId"] = record.plan.agent_id
        return evidence


def _snapshot_hash(snapshot: dict) -> str:
    """计算规范化快照哈希，防止相同版本标识下的内容漂移被忽略。"""
    import json

    normalized = json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(normalized).hexdigest()


def _sha256_json(value: object) -> str:
    """对命令 argv 等结构化输入求稳定摘要，审计可比对但不会把原始命令散落到结果页。"""
    normalized = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _sha256_text(value: str) -> str:
    """仅保存输出内容哈希，避免工具输出中的凭证或业务数据进入长期实验记录。"""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _evidence_ids(result: dict) -> list[str]:
    """从 Runtime 结果提取证据标识，供 Governance 计算检索类指标。"""
    evidence = result.get("evidence") or result.get("knowledge_evidence") or []
    return [
        str(item.get("id") or item.get("chunk_id"))
        for item in evidence
        if isinstance(item, dict) and (item.get("id") or item.get("chunk_id"))
    ]


def _candidate_answer(result: dict, target_type: str) -> str:
    """将 Agent 回答或 Skill 结构化输出转为 Judge 的稳定候选文本。"""
    if target_type == "skill":
        output = result.get("output", {})
        return json.dumps(output, ensure_ascii=False, sort_keys=True)
    return str(result.get("answer", ""))


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
    """汇总成功率、轨迹质量、成本和门禁结论，支持 Harness 策略基线比较。"""
    costs = [item.cost_usd for item in record.case_runs if item.cost_usd is not None]
    latencies = [item.latency_ms for item in record.case_runs if item.latency_ms is not None]
    trajectories = [item.trajectory for item in record.case_runs]
    recalls = [item.evidence_recall for item in trajectories if item.evidence_recall is not None]
    tool_precision = [
        item.tool_selection_precision
        for item in trajectories
        if item.tool_selection_precision is not None
    ]
    succeeded = sum(item.task_succeeded for item in trajectories)
    total = len(record.plan.cases)
    return {
        "status": record.status,
        "succeededCases": succeeded,
        "totalCases": total,
        "taskSuccessRate": succeeded / total if total else 0.0,
        "totalKnownCostUsd": sum(costs),
        "averageKnownCostUsd": sum(costs) / len(costs) if costs else None,
        "averageLatencyMs": sum(latencies) / len(latencies) if latencies else None,
        "totalToolCalls": sum(item.tool_call_count for item in trajectories),
        "totalModelCalls": sum(item.model_call_count for item in trajectories),
        "totalRetrievalRounds": sum(item.retrieval_round_count for item in trajectories),
        "humanApprovalRate": (
            sum(item.approval_count > 0 for item in trajectories) / len(trajectories)
            if trajectories
            else 0.0
        ),
        "recoveryEventCount": sum(item.recovery_event_count for item in trajectories),
        "permissionViolationCount": sum(
            item.permission_violation_count for item in trajectories
        ),
        "averageEvidenceRecall": sum(recalls) / len(recalls) if recalls else None,
        "averageToolSelectionPrecision": (
            sum(tool_precision) / len(tool_precision) if tool_precision else None
        ),
        "qualityGatePassed": _gate_passed(record),
    }


def _trajectory_metrics(
    events: list[dict],
    *,
    expected_evidence_ids: list[str],
    expected_tool_names: list[str],
    actual_evidence_ids: list[str],
    status: str,
) -> CaseTrajectoryMetrics:
    """从追加事件计算确定性指标；正文缺失时仍可依据事件类型稳定复现。"""
    event_types = [str(item.get("event_type", "")) for item in events]
    tool_events = [
        item for item in events if str(item.get("event_type", "")) == "runtime.tool.intent_recorded"
    ]
    selected_tools = [
        str((item.get("metadata") or {}).get("tool_name", ""))
        for item in tool_events
        if isinstance(item.get("metadata"), dict)
    ]
    expected_evidence = set(expected_evidence_ids)
    expected_tools = set(expected_tool_names)
    actual_evidence = set(actual_evidence_ids)
    selected = {item for item in selected_tools if item}
    violation_markers = ("permission", "forbidden", "unauthorized", "policy_denied")
    violations = sum(
        any(marker in str(item.get("metadata", {})).lower() for marker in violation_markers)
        for item in events
    )
    return CaseTrajectoryMetrics(
        task_succeeded=status in {"COMPLETED", "SUCCEEDED"},
        tool_call_count=len(tool_events),
        model_call_count=event_types.count("runtime.model.requested"),
        retrieval_round_count=event_types.count("runtime.context.injected"),
        approval_count=event_types.count("runtime.run.waiting_approval"),
        recovery_event_count=sum(
            event_types.count(item)
            for item in (
                "runtime.tool.dispatch_deferred",
                "runtime.turn.interrupted",
                "runtime.run.input_received",
            )
        ),
        permission_violation_count=violations,
        evidence_recall=(
            len(expected_evidence & actual_evidence) / len(expected_evidence)
            if expected_evidence
            else None
        ),
        tool_selection_precision=(
            len(expected_tools & selected) / len(selected)
            if expected_tools and selected
            else (1.0 if expected_tools == selected else None)
        ),
    )


def _gate_passed(record: ExperimentRecord) -> bool | None:
    """从 Governance 返回体读取唯一可信的门禁结论。"""
    return None if record.quality_gate is None else bool(record.quality_gate.get("passed"))
