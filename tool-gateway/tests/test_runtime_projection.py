"""Gateway 只加载已发布 Tool Runtime Projection 的回归。"""

import json
from pathlib import Path

from app.core.config import Settings
from app.runtime_projection import load_runtime_projection


def test_gateway_loads_persisted_runtime_projection_without_a_management_catalog(
    tmp_path: Path,
) -> None:
    """控制面不可用时，Gateway 只能使用已验证缓存，而不会回读 Draft。"""
    source = Path(__file__).parents[2] / "agent-control-plane" / "config" / "tool-catalog.json"
    cache = tmp_path / "projection.json"
    payload = json.loads(source.read_text(encoding="utf-8"))
    cache.write_text(json.dumps({"tools": payload["tools"]}), encoding="utf-8")
    settings = Settings(catalog_projection_mode="file", catalog_projection_cache_path=cache)

    registry = load_runtime_projection(settings)

    spec, _ = registry.resolve("controlled_scan", "1.0.0")
    assert spec.version == "1.0.0"
