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
        if not self.path.exists():
            raise ValueError(f"Tool Catalog is unavailable: {self.path}")
        catalog = json.loads(self.path.read_text(encoding="utf-8"))
        self.registry.validate("tool-catalog.v1.json", catalog)
        available = {(str(item["name"]), str(item["version"])) for item in catalog["tools"]}
        missing = [
            f"{item.get('tool_name')}:{item.get('version')}"
            for item in bindings
            if (str(item.get("tool_name")), str(item.get("version"))) not in available
        ]
        if missing:
            raise ValueError("published tools are absent from Tool Catalog: " + ", ".join(missing))
