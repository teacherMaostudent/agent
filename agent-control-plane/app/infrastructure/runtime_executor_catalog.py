"""版本化 Runtime 执行器目录与实例能力证明校验。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import httpx


class RuntimeExecutorCatalog:
    """读取部署目录并在发布前确认目标 Runtime 真实具备执行器 Profile。"""

    def __init__(self, path: Path, *, required: bool, timeout: float, service_key: str) -> None:
        """配置不可变目录位置、生产强制开关与内部能力探测凭据。"""
        self.path = path
        self.required = required
        self.timeout = timeout
        self.service_key = service_key

    def validate(self, environment: str, profile: str) -> dict[str, Any]:
        """验证环境允许 Profile，且有登记实例证明目录版本和执行器均一致。"""
        # Local/CI does not contact a Runtime cluster merely because the sample
        # catalog is present. Production explicitly enables this release gate.
        if not self.required:
            return {"catalog_version": "local-unchecked", "clusters": []}
        catalog = self._load()
        catalog_version = str(catalog["version"])
        eligible = [
            item
            for item in catalog["clusters"]
            if item.get("environment") == environment
            and profile in item.get("executor_profiles", [])
        ]
        if not eligible:
            raise ValueError(
                "Runtime executor profile "
                f"'{profile}' is not deployed for environment '{environment}'."
            )
        errors: list[str] = []
        for cluster in eligible:
            try:
                capability = self._capabilities(cluster)
                if capability.get("catalog_version") != catalog_version:
                    raise ValueError("catalog version mismatch")
                if profile not in capability.get("executor_profiles", []):
                    raise ValueError("profile missing from runtime capability")
                return {
                    "catalog_version": catalog_version,
                    "cluster_id": cluster.get("cluster_id"),
                    "catalog_hash": self._hash(catalog),
                }
            except (httpx.HTTPError, ValueError) as exc:
                errors.append(f"{cluster.get('cluster_id', 'unknown')}: {exc}")
        raise ValueError(
            "No eligible Runtime cluster proved executor availability: " + "; ".join(errors)
        )

    def _load(self) -> dict[str, Any]:
        """读取严格 JSON 目录；目录缺失或结构损坏在强制模式下拒绝发布。"""
        if not self.path.exists():
            raise ValueError(f"Runtime executor catalog is missing: {self.path}")
        try:
            catalog = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError("Runtime executor catalog is invalid JSON") from exc
        if not isinstance(catalog, dict) or not isinstance(catalog.get("clusters"), list):
            raise ValueError("Runtime executor catalog requires clusters array")
        if not isinstance(catalog.get("version"), str) or not catalog["version"].strip():
            raise ValueError("Runtime executor catalog requires version")
        return catalog

    def _capabilities(self, cluster: dict[str, Any]) -> dict[str, Any]:
        """调用目标 Runtime 内部能力接口，不以控制面静态配置替代实例事实。"""
        base_url = str(cluster.get("base_url", "")).rstrip("/")
        if not base_url:
            raise ValueError("Runtime cluster has no base_url")
        headers = {"X-Rag-Agent-Key": self.service_key} if self.service_key else {}
        response = httpx.get(
            f"{base_url}/api/v1/agent/capabilities", headers=headers, timeout=self.timeout
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("Runtime capability response is invalid")
        return payload

    @staticmethod
    def _hash(catalog: dict[str, Any]) -> str:
        """对规范 JSON 计算目录摘要，供 Release 与审计事件固定部署证据。"""
        canonical = json.dumps(catalog, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()
