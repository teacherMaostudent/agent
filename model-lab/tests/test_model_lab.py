"""Model Lab 本地契约测试：持久化记录与不可逆 Worker 结论。"""

import pytest
from app.main import (
    EvaluationResult,
    ExperimentPlan,
    ModelCard,
    ModelLabService,
    WorkerResult,
)
from app.repository import SqliteModelLabRepository
from app.settings import Settings


def _plan() -> ExperimentPlan:
    """构造包含完整可复现身份的最小 LoRA 实验计划。"""
    return ExperimentPlan(
        tenant_id="tenant-a",
        method="lora",
        base_model="bge-m3",
        base_model_revision="main@abc",
        dataset_uri="s3://datasets/train.jsonl",
        dataset_sha256="a" * 64,
        container_image_digest="sha256:" + "b" * 64,
        random_seed=7,
        parameters={"lora_rank": 8},
        evaluation_thresholds={"recall": 0.8},
    )


def test_worker_result_is_persisted_and_cannot_be_replaced(tmp_path) -> None:
    """同一冻结计划只能得到一次可审计结果，避免发布后替换模型工件。"""
    repository = SqliteModelLabRepository(Settings(database_path=tmp_path / "lab.db"))
    repository.initialize()
    service = ModelLabService(repository)
    record = service.create(_plan())
    result = WorkerResult(
        worker_id="gpu-worker-a",
        evaluation=EvaluationResult(
            metrics={"recall": 0.9},
            benchmark_version="golden/v1",
            evaluator_image_digest="sha256:" + "c" * 64,
        ),
        model_card=ModelCard(
            artifact_uri="s3://models/model.safetensors",
            artifact_sha256="d" * 64,
            license="Apache-2.0",
            intended_use="retrieval",
        ),
    )
    approved = service.record_worker_result("tenant-a", record.experiment_id, result)
    assert approved.status == "APPROVED"
    assert service.get("tenant-a", record.experiment_id).model_card == result.model_card
    with pytest.raises(ValueError, match="final evaluation"):
        service.record_worker_result("tenant-a", record.experiment_id, result)
    repository.close()
