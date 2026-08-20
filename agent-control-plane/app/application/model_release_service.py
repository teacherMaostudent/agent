"""Model-route rollout policy owned by Control Plane.

The service changes a Gateway route only after Governance has accepted the
quality evidence, and restores the recorded prior route on rollback.  Gateway
executes route policy but does not own its release lifecycle.
"""

from __future__ import annotations

import logging
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import httpx

from app.application.exceptions import InvalidStateError, NotFoundError, PolicyViolationError
from app.core.config import Settings
from app.infrastructure.platform_clients import (
    GatewayPolicyClient,
    GovernanceQualityClient,
    ModelLabClient,
)
from app.infrastructure.sqlite_repository import SqliteRepository

logger = logging.getLogger(__name__)


def _now() -> str:
    """生成带 UTC
    时区的当前时间，供状态记录、保留策略和审计排序使用，避免各调用方自行处理时区。
    """
    return datetime.now(UTC).isoformat()


class ModelReleaseService:
    """Orchestrates quality gate, canary, promotion and rollback."""

    def __init__(
        self,
        repository: SqliteRepository,
        settings: Settings,
        gateway: GatewayPolicyClient,
        governance: GovernanceQualityClient,
        model_lab: ModelLabClient | None = None,
    ) -> None:
        """注入发布仓储、质量门禁、Gateway 与可选 Model Lab
        客户端；该对象只编排模型路由生命周期，不执行模型请求。
        """
        self._repository = repository
        self._settings = settings
        self._gateway = gateway
        self._governance = governance
        self._model_lab = model_lab

    async def start(self, tenant_id: str, request: dict[str, Any]) -> dict[str, Any]:
        """启动有界灰度发布：先读取旧路由并核验 Governance Gate 与
        Model Lab 工件，再写入候选流量；门禁未通过时只保存拒绝事实。

        Start a bounded canary only when the referenced quality gate passed.
        """
        route_name = _required(request, "routeName")
        target = _required(request, "canaryTarget")
        if ":" not in target:
            raise PolicyViolationError("canaryTarget must use provider:model format.")
        percent = int(request.get("canaryPercent") or 5)
        if percent < 1 or percent > 50:
            raise PolicyViolationError("canaryPercent must be between 1 and 50.")
        previous = await self._gateway.route(route_name)
        gate = await self._governance.quality_gate(tenant_id, _required(request, "judgeRunId"))
        experiment_id = str(request.get("modelLabExperimentId") or "")
        model_artifact = None
        if self._settings.model_lab_required and not experiment_id:
            raise PolicyViolationError("modelLabExperimentId is required for model route releases")
        if experiment_id:
            if self._model_lab is None:
                raise PolicyViolationError("Model Lab validation is not configured")
            try:
                model_artifact = await self._model_lab.approved_artifact(tenant_id, experiment_id)
            except (httpx.HTTPError, ValueError) as exc:
                raise PolicyViolationError(f"Model Lab gate rejected release: {exc}") from exc
        now = _now()
        release = {
            "id": uuid4().hex,
            "routeName": route_name,
            "canaryTarget": target,
            "canaryPercent": percent,
            "status": "QUALITY_GATE_REJECTED" if not gate.get("passed") else "CANARY_ACTIVE",
            "qualityGateId": gate.get("id"),
            "modelLabExperimentId": experiment_id or None,
            "modelArtifact": (model_artifact or {}).get("model_card"),
            "startedAt": now,
            "updatedAt": now,
            "previousRoute": previous,
            "metrics": gate.get("metrics") or {},
            "reasons": gate.get("reasons") or [],
        }
        if gate.get("passed") and (not experiment_id or model_artifact is not None):
            canary = deepcopy(previous)
            canary["canary"] = [{"target": target, "percent": percent}]
            await self._gateway.upsert_route(route_name, canary)
        return await self._repository.save_model_release(tenant_id, release["id"], release)

    async def monitor(self, tenant_id: str, release_id: str) -> dict[str, Any]:
        """读取 Gateway 的真实灰度指标，并按样本量、错误率、超时率和延迟决定继续观
        察、晋级或恢复已记录的旧路由。

        Promote, continue, or roll back using recorded—not inferred—baseline state.
        """
        release = await self.get(tenant_id, release_id)
        if release["status"] not in {"CANARY_ACTIVE", "MONITORING"}:
            return release
        summary = await self._gateway.performance_summary(
            datetime.fromisoformat(release["startedAt"]),
            release["routeName"],
            release["canaryTarget"],
        )
        metrics = {
            "requests": int(summary.get("requests", 0)),
            "errorRate": float(summary.get("errorRate", 0)),
            "timeoutRate": float(summary.get("timeoutRate", 0)),
            "avgLatencyMs": int(summary.get("avgLatencyMs", 0)),
            "costPerRequest": summary.get("costPerRequest", 0),
        }
        reasons = []
        if metrics["errorRate"] > self._settings.model_release_max_error_rate:
            reasons.append("errorRate exceeded configured maximum")
        if metrics["timeoutRate"] > self._settings.model_release_max_timeout_rate:
            reasons.append("timeoutRate exceeded configured maximum")
        if metrics["avgLatencyMs"] > self._settings.model_release_max_average_latency_ms:
            reasons.append("avgLatencyMs exceeded configured maximum")
        if reasons:
            return await self._rollback(tenant_id, release, metrics, reasons)
        if metrics["requests"] < self._settings.model_release_min_canary_requests:
            return await self._save(
                tenant_id,
                release,
                "MONITORING",
                metrics,
                ["waiting for minimum canary sample size"],
            )
        if not self._settings.model_release_auto_promote:
            return await self._save(
                tenant_id,
                release,
                "MONITORING",
                metrics,
                ["canary healthy; manual promotion required"],
            )
        return await self._promote(tenant_id, release, metrics)

    async def rollback(self, tenant_id: str, release_id: str, reason: str) -> dict[str, Any]:
        """执行人工回滚入口；原因与观测指标一并持久化，恢复目标来自发布前快照而不是当前路由
        推断。
        """
        release = await self.get(tenant_id, release_id)
        return await self._rollback(tenant_id, release, {}, [reason or "manual rollback"])

    async def get(self, tenant_id: str, release_id: str) -> dict[str, Any]:
        """按租户读取模型路由发布；不存在时返回明确的
        NotFound，禁止跨租户猜测发布状态。
        """
        release = await self._repository.get_model_release(tenant_id, release_id)
        if not release:
            raise NotFoundError(f"Unknown model route release: {release_id}")
        return release

    async def list(self, tenant_id: str) -> list[dict[str, Any]]:
        """列出当前租户的模型路由发布记录，不触发监控、晋级或任何 Gateway 写操作。"""
        return await self._repository.list_model_releases(tenant_id)

    async def monitor_active(self) -> None:
        """扫描所有进行中的模型发布并逐一评估；单个发布失败只记录结构化错误，不阻断其他租户
        。
        """
        for tenant_id, release in await self._repository.list_active_model_releases():
            try:
                await self.monitor(tenant_id, release["id"])
            except Exception:
                logger.exception(
                    "model_release_monitor_failed",
                    extra={"tenant_id": tenant_id, "release_id": release["id"]},
                )

    async def _promote(
        self, tenant_id: str, release: dict[str, Any], metrics: dict[str, Any]
    ) -> dict[str, Any]:
        """将已通过观察窗口的候选模型提升为主路由，并把旧主模型保留为首选回退目标。"""
        current = await self._gateway.route(release["routeName"])
        old_primary = current.get("primary")
        promoted = deepcopy(current)
        promoted["primary"] = release["canaryTarget"]
        promoted["canary"] = []
        fallbacks = list(promoted.get("fallbacks") or [])
        if old_primary and old_primary != promoted["primary"] and old_primary not in fallbacks:
            fallbacks.insert(0, old_primary)
        promoted["fallbacks"] = fallbacks
        await self._gateway.upsert_route(release["routeName"], promoted)
        return await self._save(tenant_id, release, "PROMOTED", metrics, [])

    async def _rollback(
        self,
        tenant_id: str,
        release: dict[str, Any],
        metrics: dict[str, Any],
        reasons: list[str],
    ) -> dict[str, Any]:
        """恢复发布开始前记录的完整路由快照；已正式晋级的发布禁止就地回滚，必须新建发布。"""
        if release["status"] == "PROMOTED":
            raise InvalidStateError("A promoted route requires a new release to roll back.")
        await self._gateway.upsert_route(release["routeName"], release["previousRoute"])
        return await self._save(tenant_id, release, "ROLLED_BACK", metrics, reasons)

    async def _save(
        self,
        tenant_id: str,
        release: dict[str, Any],
        status: str,
        metrics: dict[str, Any],
        reasons: list[str],
    ) -> dict[str, Any]:
        """持久化模型发布的新状态、指标、原因和更新时间，确保监控结果可审计。"""
        updated = {
            **release,
            "status": status,
            "metrics": metrics,
            "reasons": reasons,
            "updatedAt": _now(),
        }
        return await self._repository.save_model_release(tenant_id, release["id"], updated)


def _required(request: dict[str, Any], name: str) -> str:
    """读取并去除必填发布字段的空白；缺失字段在调用 Governance 或
    Gateway 前以策略错误拒绝。
    """
    value = str(request.get(name) or "").strip()
    if not value:
        raise PolicyViolationError(f"{name} is required.")
    return value
