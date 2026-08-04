from __future__ import annotations

import logging
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from app.application.exceptions import InvalidStateError, NotFoundError, PolicyViolationError
from app.core.config import Settings
from app.infrastructure.platform_clients import GatewayPolicyClient, GovernanceQualityClient
from app.infrastructure.sqlite_repository import SqliteRepository

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(UTC).isoformat()


class ModelReleaseService:
    """Orchestrates quality gate, canary, promotion and rollback."""

    def __init__(
        self,
        repository: SqliteRepository,
        settings: Settings,
        gateway: GatewayPolicyClient,
        governance: GovernanceQualityClient,
    ) -> None:
        self._repository = repository
        self._settings = settings
        self._gateway = gateway
        self._governance = governance

    async def start(self, tenant_id: str, request: dict[str, Any]) -> dict[str, Any]:
        route_name = _required(request, "routeName")
        target = _required(request, "canaryTarget")
        if ":" not in target:
            raise PolicyViolationError("canaryTarget must use provider:model format.")
        percent = int(request.get("canaryPercent") or 5)
        if percent < 1 or percent > 50:
            raise PolicyViolationError("canaryPercent must be between 1 and 50.")
        previous = await self._gateway.route(route_name)
        gate = await self._governance.quality_gate(tenant_id, _required(request, "judgeRunId"))
        now = _now()
        release = {
            "id": uuid4().hex,
            "routeName": route_name,
            "canaryTarget": target,
            "canaryPercent": percent,
            "status": "QUALITY_GATE_REJECTED" if not gate.get("passed") else "CANARY_ACTIVE",
            "qualityGateId": gate.get("id"),
            "startedAt": now,
            "updatedAt": now,
            "previousRoute": previous,
            "metrics": gate.get("metrics") or {},
            "reasons": gate.get("reasons") or [],
        }
        if gate.get("passed"):
            canary = deepcopy(previous)
            canary["canary"] = [{"target": target, "percent": percent}]
            await self._gateway.upsert_route(route_name, canary)
        return await self._repository.save_model_release(tenant_id, release["id"], release)

    async def monitor(self, tenant_id: str, release_id: str) -> dict[str, Any]:
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
        release = await self.get(tenant_id, release_id)
        return await self._rollback(tenant_id, release, {}, [reason or "manual rollback"])

    async def get(self, tenant_id: str, release_id: str) -> dict[str, Any]:
        release = await self._repository.get_model_release(tenant_id, release_id)
        if not release:
            raise NotFoundError(f"Unknown model route release: {release_id}")
        return release

    async def list(self, tenant_id: str) -> list[dict[str, Any]]:
        return await self._repository.list_model_releases(tenant_id)

    async def monitor_active(self) -> None:
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
        updated = {
            **release,
            "status": status,
            "metrics": metrics,
            "reasons": reasons,
            "updatedAt": _now(),
        }
        return await self._repository.save_model_release(tenant_id, release["id"], updated)


def _required(request: dict[str, Any], name: str) -> str:
    value = str(request.get(name) or "").strip()
    if not value:
        raise PolicyViolationError(f"{name} is required.")
    return value
