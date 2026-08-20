"""Release-time verification that published tool bindings actually exist.

The Runtime only receives a snapshot after this validation passes in production,
preventing an agent release from naming a deleted or unreviewed tool version.
"""

from __future__ import annotations

import json
from pathlib import Path

from platform_infra.schema_registry import SchemaRegistry


class ToolCatalogValidator:
    """Validates bindings against the same catalog loaded by Tool Gateway."""

    def __init__(
        self,
        path: str | Path,
        schema_dir: str | Path,
        *,
        required: bool = False,
    ) -> None:
        """加载目录和 Schema 注册表，并记录生产环境是否必须执行绑定校验。"""
        self.path = Path(path)
        self.registry = SchemaRegistry(schema_dir)
        self.required = required

    def validate_bindings(self, bindings: list[dict]) -> None:
        """发布前验证工具名和版本在受 Schema 约束的目录中存在，拒绝漂移绑定。"""
        if not self.required:
            # Local/test deployments may not mount the production Catalog;
            # production configuration turns this check on explicitly.
            return
        catalog = self._load_catalog()
        available = {(str(item["name"]), str(item["version"])) for item in catalog["tools"]}
        missing = [
            f"{item.get('tool_name')}:{item.get('version')}"
            for item in bindings
            if (str(item.get("tool_name")), str(item.get("version"))) not in available
        ]
        if missing:
            raise ValueError("published tools are absent from Tool Catalog: " + ", ".join(missing))

    def resolve_bindings(self, bindings: list[dict]) -> list[dict]:
        """以 Catalog 为风险与权限事实源，返回可冻结进 Snapshot 的精确绑定。"""
        if not self.required:
            return [dict(item) for item in bindings]
        catalog = self._load_catalog()
        indexed = {(str(item["name"]), str(item["version"])): item for item in catalog["tools"]}
        resolved: list[dict] = []
        for binding in bindings:
            key = (str(binding.get("tool_name")), str(binding.get("version")))
            catalog_item = indexed.get(key)
            if catalog_item is None:
                raise ValueError(f"published tools are absent from Tool Catalog: {key[0]}:{key[1]}")
            value = dict(binding)
            value.update(
                {
                    "risk": catalog_item.get("risk", "read_only"),
                    "approval_required": bool(catalog_item.get("approval_required", False)),
                    "idempotent": bool(catalog_item.get("idempotent", False)),
                    "required_permissions": list(catalog_item.get("required_permissions", [])),
                }
            )
            value["side_effect"] = value["risk"] != "read_only"
            resolved.append(value)
        return resolved

    def resolve_catalog_items(self, bindings: list[dict]) -> list[dict]:
        """返回精确目录项供 Skill/Workflow 校验风险，调用者不得把它写成另一份事实源。"""
        if not self.required:
            return []
        catalog = self._load_catalog()
        indexed = {(str(item["name"]), str(item["version"])): item for item in catalog["tools"]}
        result = []
        for binding in bindings:
            key = (str(binding.get("tool_name")), str(binding.get("version")))
            if key not in indexed:
                raise ValueError(f"published tools are absent from Tool Catalog: {key[0]}:{key[1]}")
            result.append(dict(indexed[key]))
        return result

    def _load_catalog(self) -> dict:
        """只在发布检查时读取并验证目录，所有校验方法共享同一解析规则。"""
        if not self.path.exists():
            raise ValueError(f"Tool Catalog is unavailable: {self.path}")
        catalog = json.loads(self.path.read_text(encoding="utf-8"))
        self.registry.validate("tool-catalog.v1.json", catalog)
        return catalog
