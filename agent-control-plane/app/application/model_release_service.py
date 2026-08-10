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
    """Internal helper for module; preserve its caller-facing invariant."""
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
        """Initialize ModelReleaseService dependencies and local state."""
        self._repository = repository
        self._settings = settings
        self._gateway = gateway
        self._governance = governance
        self._model_lab = model_lab

    async def start(self, tenant_id: str, request: dict[str, Any]) -> dict[str, Any]:
        """Start a bounded canary only when the referenced quality gate passed."""
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
                model_artifact = await self._model_lab.approved_artifact(experiment_id)
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
        """Promote, continue, or roll back using recorded—not inferred—baseline state."""
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
        """Perform rollback within the ModelReleaseService ownership boundary."""
        release = await self.get(tenant_id, release_id)
        return await self._rollback(tenant_id, release, {}, [reason or "manual rollback"])

    async def get(self, tenant_id: str, release_id: str) -> dict[str, Any]:
        """Perform get within the ModelReleaseService ownership boundary."""
        release = await self._repository.get_model_release(tenant_id, release_id)
        if not release:
            raise NotFoundError(f"Unknown model route release: {release_id}")
        return release

    async def list(self, tenant_id: str) -> list[dict[str, Any]]:
        """Perform list within the ModelReleaseService ownership boundary."""
        return await self._repository.list_model_releases(tenant_id)

    async def monitor_active(self) -> None:
        """Run the bounded monitor active operation and surface failures."""
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
        """Internal helper for ModelReleaseService; preserve its caller-facing invariant."""
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
        """Internal helper for ModelReleaseService; preserve its caller-facing invariant."""
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
        """Internal helper for ModelReleaseService; preserve its caller-facing invariant."""
        updated = {
            **release,
            "status": status,
            "metrics": metrics,
            "reasons": reasons,
            "updatedAt": _now(),
        }
        return await self._repository.save_model_release(tenant_id, release["id"], updated)


def _required(request: dict[str, Any], name: str) -> str:
    """Internal helper for module; preserve its caller-facing invariant."""
    value = str(request.get(name) or "").strip()
    if not value:
        raise PolicyViolationError(f"{name} is required.")
    return value
