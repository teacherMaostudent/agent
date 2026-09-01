"""Control Plane 生成并校验 Tool Gateway 可消费的只读运行时投影。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from platform_infra.schema_registry import SchemaRegistry


class ToolRuntimeProjectionPublisher:
    """把已发布工具定义投影为 Gateway 所需的最小不可变执行工件。"""

    def __init__(self, repository, schema_dir: str | Path) -> None:
        self._repository = repository
        self._schemas = SchemaRegistry(schema_dir)

    async def current(self) -> dict[str, Any]:
        """只投影 Active Runtime Release；Draft、Review 和已弃用定义无法穿越边界。"""
        versions = await self._repository.list_published_tool_versions()
        catalog = {"tools": [item.runtime_definition for item in versions]}
        self._schemas.validate("tool-catalog.v1.json", catalog)
        canonical = json.dumps(catalog, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        release_material = [
            {
                "tenant_id": item.tenant_id,
                "tool_id": item.tool_id,
                "version": item.semantic_version,
                "content_sha256": item.content_sha256,
            }
            for item in versions
        ]
        return {
            "projection_version": "tool-runtime-projection/v1",
            "catalog_release_id": "tool-catalog-"
            + hashlib.sha256(
                json.dumps(release_material, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()[:16],
            "content_sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
            "catalog": catalog,
        }
