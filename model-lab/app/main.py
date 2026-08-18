"""Model Lab：离线模型工件的受控实验登记、Worker 回传与发布证据接口。"""

from __future__ import annotations

import secrets
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from platform_infra.identity import OidcIdentityMiddleware
from platform_infra.telemetry import configure_telemetry
from pydantic import BaseModel, Field, model_validator

from app.repository import ModelLabRepository, build_repository
from app.settings import Settings


class ExperimentPlan(BaseModel):
    """由独立 GPU Worker 消费的可复现实验计划；API 本身绝不训练模型。"""

    tenant_id: str = Field(min_length=1, max_length=160)
    method: Literal["lora", "qlora", "dpo", "grpo", "distributed"]
    base_model: str = Field(min_length=1, max_length=300)
    base_model_revision: str = Field(min_length=1, max_length=200)
    dataset_uri: str = Field(min_length=1, max_length=2_000)
    dataset_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    container_image_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    random_seed: int
    parameters: dict[str, object] = Field(default_factory=dict)
    evaluation_thresholds: dict[str, float] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_method_parameters(self) -> ExperimentPlan:
        """要求每种训练方法提供不可省略的复现输入，Worker 不得自行猜测。"""
        if self.method in {"lora", "qlora"} and "lora_rank" not in self.parameters:
            raise ValueError("LoRA/QLoRA plans require parameters.lora_rank")
        if self.method in {"dpo", "grpo"} and "preference_dataset_uri" not in self.parameters:
            raise ValueError("DPO/GRPO plans require a preference dataset")
        if self.method == "distributed" and "launcher" not in self.parameters:
            raise ValueError("distributed plans require parameters.launcher")
        return self


class EvaluationResult(BaseModel):
    """由固定版本评测镜像生成的数值结果，不能只依赖 LLM Judge。"""

    metrics: dict[str, float]
    benchmark_version: str = Field(min_length=1, max_length=160)
    evaluator_image_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")


class ModelCard(BaseModel):
    """可发布模型工件的不可变身份、使用范围和限制声明。"""

    artifact_uri: str = Field(min_length=1, max_length=2_000)
    artifact_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    quantization: str | None = Field(default=None, max_length=80)
    license: str = Field(min_length=1, max_length=300)
    intended_use: str = Field(min_length=1, max_length=2_000)
    limitations: list[str] = Field(default_factory=list)


class WorkerResult(BaseModel):
    """GPU Worker 回传的工件和评测证据；不能修改冻结的训练计划。"""

    evaluation: EvaluationResult
    model_card: ModelCard
    worker_id: str = Field(min_length=1, max_length=160)


class ExperimentRecord(BaseModel):
    """一个冻结计划及其唯一模型工件结论的持久化聚合。"""

    experiment_id: str
    plan: ExperimentPlan
    status: Literal["PLANNED", "RUNNING", "REJECTED", "APPROVED"] = "PLANNED"
    evaluation: EvaluationResult | None = None
    model_card: ModelCard | None = None
    worker_id: str = ""
    created_at: datetime
    updated_at: datetime


class ModelLabService:
    """收口实验状态迁移；Worker、Control Plane 和路由不可绕过此领域边界。"""

    def __init__(self, repository: ModelLabRepository) -> None:
        """保存持久化端口，允许本地与生产后端共用相同业务规则。"""
        self._repository = repository

    def create(self, plan: ExperimentPlan) -> ExperimentRecord:
        """登记不可变训练计划；后续仅 Worker 可以产生与之绑定的结果。"""
        now = datetime.now(UTC)
        return self._repository.create(
            ExperimentRecord(experiment_id=f"exp_{uuid4().hex}", plan=plan, created_at=now, updated_at=now)
        )

    def get(self, tenant_id: str, experiment_id: str) -> ExperimentRecord:
        """按租户读取实验，不存在时不泄露其他租户是否持有该工件。"""
        record = self._repository.get(tenant_id, experiment_id)
        if record is None:
            raise KeyError(experiment_id)
        return record

    def begin(self, tenant_id: str, experiment_id: str) -> ExperimentRecord:
        """由受信 Worker 标记已开始的计划；终态工件禁止重新训练。"""
        record = self.get(tenant_id, experiment_id)
        if record.status == "PLANNED":
            record.status, record.updated_at = "RUNNING", datetime.now(UTC)
            return self._repository.save(record)
        if record.status == "RUNNING":
            return record
        raise ValueError("experiment is already finalized")

    def record_worker_result(self, tenant_id: str, experiment_id: str, result: WorkerResult) -> ExperimentRecord:
        """对冻结门槛做确定性判定，同一实验只接受一次不可变 Worker 结果。"""
        record = self.get(tenant_id, experiment_id)
        if record.status not in {"PLANNED", "RUNNING"}:
            raise ValueError("experiment already has a final evaluation")
        if self._repository.settings.environment.lower() in {"production", "prod"}:
            expected_prefix = f"s3://{self._repository.settings.artifact_bucket}/"
            if not result.model_card.artifact_uri.startswith(expected_prefix):
                raise ValueError("production model artifact must be stored in the configured object bucket")
        failed = [
            name for name, threshold in record.plan.evaluation_thresholds.items()
            if result.evaluation.metrics.get(name, float("-inf")) < threshold
        ]
        record.evaluation, record.model_card, record.worker_id = result.evaluation, result.model_card, result.worker_id
        record.status, record.updated_at = ("REJECTED" if failed else "APPROVED"), datetime.now(UTC)
        return self._repository.save(record)


def create_app(settings: Settings | None = None) -> FastAPI:
    """装配受保护 API 与持久化服务；生产实例不允许进程内实验状态。"""
    resolved = settings or Settings()
    repository = build_repository(resolved)
    service = ModelLabService(repository)

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        """在接流量前初始化持久化结构，停止时释放后端资源。"""
        repository.initialize()
        try:
            yield
        finally:
            repository.close()

    application = FastAPI(title="Agent Platform Model Lab", version="1.0.0", lifespan=lifespan)
    application.state.repository, application.state.service = repository, service
    application.add_middleware(OidcIdentityMiddleware, enabled=resolved.oidc_enabled, issuer=resolved.oidc_issuer, audience=resolved.oidc_audience, jwks_url=resolved.oidc_jwks_url, public_paths=("/health/live", "/health/ready"), trusted_workload_prefixes=())
    configure_telemetry(application, enabled=resolved.otel_enabled, service_name="model-lab", environment=resolved.environment, endpoint=resolved.otel_endpoint)
    return application


app = create_app()


def _service(request: Request) -> ModelLabService:
    """取得应用单例服务，路由不自行创建数据库或 Worker 状态。"""
    return request.app.state.service


def _require_internal_key(request: Request, x_model_lab_key: str | None = Header(default=None)) -> None:
    """保护 Control Plane 读取与 GPU Worker 写入；密钥作为 OIDC/mTLS 的纵深防御。"""
    expected = request.app.state.repository.settings.service_api_key
    if not expected or not secrets.compare_digest(x_model_lab_key or "", expected):
        raise HTTPException(401, "invalid Model Lab service credential")


def _tenant(request: Request, tenant_id: str) -> str:
    """OIDC 启用后要求 URL 租户与经验证身份一致，拒绝参数越权。"""
    if request.app.state.repository.settings.oidc_enabled and request.headers.get("X-Tenant-Id", "") != tenant_id:
        raise HTTPException(403, "tenant does not match authenticated identity")
    return tenant_id


@app.get("/health/live")
def liveness() -> dict[str, str]:
    """报告进程存活，不将下游短暂故障误判为实例退出。"""
    return {"status": "ok"}


@app.get("/health/ready")
def readiness(request: Request) -> dict[str, str]:
    """报告已装配后端，帮助探针区分本地与生产持久化模式。"""
    return {"status": "ok", "persistence": request.app.state.repository.backend}


@app.post("/v1/experiments", response_model=ExperimentRecord, status_code=201)
def create_experiment(plan: ExperimentPlan, request: Request) -> ExperimentRecord:
    """终端用户为自己的租户登记冻结训练计划。"""
    _tenant(request, plan.tenant_id)
    return _service(request).create(plan)


@app.post("/internal/v1/experiments/{experiment_id}/begin", response_model=ExperimentRecord)
def begin_experiment(experiment_id: str, tenant_id: str, request: Request, _: None = Depends(_require_internal_key)) -> ExperimentRecord:
    """受信 Worker 标记计划已开始；不会改写训练输入。"""
    return _service(request).begin(_tenant(request, tenant_id), experiment_id)


@app.post("/internal/v1/experiments/{experiment_id}/results", response_model=ExperimentRecord)
def record_worker_result(experiment_id: str, tenant_id: str, result: WorkerResult, request: Request, _: None = Depends(_require_internal_key)) -> ExperimentRecord:
    """接收固定评测/工件 manifest，再依据计划门槛批准或拒绝。"""
    return _service(request).record_worker_result(_tenant(request, tenant_id), experiment_id, result)


@app.get("/internal/v1/experiments/{experiment_id}", response_model=ExperimentRecord)
def approved_artifact(experiment_id: str, tenant_id: str, request: Request, _: None = Depends(_require_internal_key)) -> ExperimentRecord:
    """向 Control Plane 返回同租户实验事实，状态是否可发布由调用方拒绝判断。"""
    return _service(request).get(_tenant(request, tenant_id), experiment_id)
