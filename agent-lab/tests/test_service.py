from pathlib import Path

from app.main_settings import Settings
from app.models import EvaluationBinding, ExperimentJobStatus, ExperimentPlan, ReplayCase
from app.repository import ExperimentRepository
from app.service import AgentLabService
from app.worker import AgentLabWorker


class FakeControlPlane:
    """提供稳定发布快照的契约假件，避免单元测试依赖真实 Control Plane。"""

    def resolve(self, tenant_id, agent_id, environment, session_id):
        """返回所有用例都相同的冻结版本，模拟稳定 laboratory 发布。"""
        del tenant_id, environment, session_id
        return {
            "release_id": "release-1",
            "version_id": "version-1",
            "snapshot": {"agent_id": agent_id, "version": "1.0.0"},
        }


class FakeRuntime:
    """提供可重复 Runtime 响应的测试替身。"""

    def run(self, payload, tenant_id, session_id, request_id):
        """按任务构造带证据、成本和稳定运行标识的成功结果。"""
        del tenant_id, session_id
        return {
            "run_id": request_id,
            "status": "SUCCEEDED",
            "answer": f"answer for {payload['task']}",
            "evidence": [{"chunk_id": "chunk-1"}],
            "latency_ms": 12,
            "cost_usd": 0.01,
        }


class FakeGovernance:
    """提供已通过门禁的 Governance 假件，验证 Lab 不自行判断质量。"""

    def judge(self, tenant_id, request):
        """返回固定 Judge Run 标识。"""
        del tenant_id, request
        return {"id": "judge-1"}

    def quality_gate(self, tenant_id, run_id, request):
        """返回通过的 Hard Gate 结论。"""
        del tenant_id, run_id, request
        return {"id": "gate-1", "passed": True}


def test_replay_freezes_snapshot_runs_cases_and_delegates_gate(tmp_path: Path):
    """直接回放仍可作为本地契约测试，成功结果必须保留冻结快照与治理门禁。"""
    repository = ExperimentRepository(tmp_path / "lab.db")
    service = AgentLabService(
        repository, FakeControlPlane(), FakeRuntime(), FakeGovernance(), max_cases=10
    )
    record = service.create(
        ExperimentPlan(
            name="smoke",
            tenant_id="tenant-a",
            agent_id="general-agent",
            cases=[ReplayCase(case_id="case-1", task="explain this")],
            evaluation=EvaluationBinding(),
        )
    )

    completed = service.run("tenant-a", record.experiment_id)

    assert completed.status == "COMPLETED"
    assert completed.snapshot_bindings[0].version_id == "version-1"
    assert completed.case_runs[0].answer == "answer for explain this"
    assert completed.quality_gate == {"id": "gate-1", "passed": True}
    assert service.release_evidence("tenant-a", record.experiment_id) == {
        "experimentId": record.experiment_id,
        "tenantId": "tenant-a",
        "agentId": "general-agent",
        "environment": "laboratory",
        "versionId": "version-1",
        "releaseId": "release-1",
        "snapshotHash": completed.snapshot_bindings[0].snapshot_hash,
        "judgeRunId": "judge-1",
        "qualityGate": {"id": "gate-1", "passed": True},
    }


def test_skill_replay_resolves_exact_artifact_and_uses_governed_runtime(tmp_path: Path):
    class SkillControlPlane(FakeControlPlane):
        def resolve_skill(self, tenant_id, skill_id, version):
            assert (tenant_id, skill_id, version) == (
                "tenant-a",
                "document-review",
                "1.0.0",
            )
            return {
                "version": "1.0.0",
                "artifact_digest": "a" * 64,
                "plan": {"skill_id": skill_id},
            }

    class SkillRuntime(FakeRuntime):
        def run_skill(self, payload, tenant_id, request_id):
            assert payload["artifact_digest"] == "a" * 64
            assert payload["capability_id"] == "DOCUMENT_REVIEW"
            return {"status": "SUCCEEDED", "output": {"answer": "reviewed"}}

    service = AgentLabService(
        ExperimentRepository(tmp_path / "skill-lab.db"),
        SkillControlPlane(),
        SkillRuntime(),
        FakeGovernance(),
        max_cases=10,
    )
    record = service.create(
        ExperimentPlan(
            name="skill-replay",
            tenant_id="tenant-a",
            target_type="skill",
            skill_id="document-review",
            skill_version="1.0.0",
            skill_capability_id="DOCUMENT_REVIEW",
            cases=[ReplayCase(case_id="case-1", task="review")],
            evaluation=EvaluationBinding(),
        )
    )
    completed = service.run("tenant-a", record.experiment_id)
    assert completed.status == "COMPLETED"
    assert completed.snapshot_bindings[0].snapshot_hash == "a" * 64
    evidence = service.release_evidence("tenant-a", record.experiment_id)
    assert evidence["targetType"] == "skill"
    assert evidence["skillId"] == "document-review"


def test_worker_retries_transport_failure_then_persists_dlq(tmp_path: Path):
    """Worker 必须通过租约任务退避重试，并在超过预算后将实验和任务同时收口为失败。"""

    class UnavailableRuntime:
        """模拟 Runtime 网络暂不可用，验证它不会被错误地标记为一次成功实验。"""

        def run(self, payload, tenant_id, session_id, request_id):
            """始终以可重试传输异常失败。"""
            del payload, tenant_id, session_id, request_id
            import httpx

            raise httpx.ConnectError("runtime unavailable")

    repository = ExperimentRepository(tmp_path / "lab.db")
    service = AgentLabService(
        repository, FakeControlPlane(), UnavailableRuntime(), FakeGovernance(), max_cases=10
    )
    record = service.create(
        ExperimentPlan(
            name="retry",
            tenant_id="tenant-a",
            agent_id="general-agent",
            cases=[ReplayCase(case_id="case-1", task="explain this")],
        )
    )
    job = service.submit("tenant-a", record.experiment_id, max_attempts=2)
    worker = AgentLabWorker(
        repository,
        service,
        worker_id="worker-a",
        lease_seconds=60,
        retry_initial_seconds=0,
        retry_max_seconds=1,
    )

    first = worker.execute(job.job_id)

    assert first["status"] == ExperimentJobStatus.RETRY_SCHEDULED
    assert repository.get_job("tenant-a", job.job_id).attempt_count == 1
    # 本地 SQLite 测试 uses zero delay so the next claim can verify DLQ handling.
    second = worker.execute(job.job_id)

    assert second["status"] == ExperimentJobStatus.DLQ
    assert repository.get_job("tenant-a", job.job_id).status == ExperimentJobStatus.DLQ
    assert service.get("tenant-a", record.experiment_id).status == "FAILED"


def test_production_settings_reject_sqlite_and_missing_security():
    """生产环境不得把本地 SQLite 或缺失身份/mTLS 配置伪装成可用实验平台。"""
    import pytest

    with pytest.raises(ValueError, match="AGENT_LAB_DATABASE_BACKEND"):
        Settings(environment="production")
