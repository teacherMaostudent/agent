"""Tool Gateway 对 Control Plane 已发布工具投影的只读加载器。"""

from __future__ import annotations

import json
from typing import Any

import httpx

from app.registry import ToolRegistry, load_registry


def load_runtime_projection(
    settings: Any, *, client_options: dict[str, Any] | None = None
) -> ToolRegistry:
    """优先同步 Control Plane 投影，失败时仅可使用已落盘的不可变缓存。

    这样控制面临时不可用不会中断已发布工具；首次启动没有有效投影则 fail-closed，
    不会退回到 Gateway 自行维护的 Draft 或可编辑目录。
    """
    cache_path = settings.catalog_projection_cache_path
    if settings.catalog_projection_mode == "control_plane":
        try:
            catalog = _fetch(settings)
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(
                json.dumps(catalog, ensure_ascii=False, sort_keys=True), encoding="utf-8"
            )
        except (httpx.HTTPError, ValueError, OSError) as exc:
            if not cache_path.exists():
                raise RuntimeError(
                    "Tool Runtime Projection is unavailable and no verified cache exists"
                ) from exc
    return load_registry(
        cache_path,
        allow_private_networks=settings.allow_private_networks,
        max_response_bytes=settings.max_response_bytes,
        schema_dir=settings.contracts_schema_dir,
        client_options=client_options,
    )


def _fetch(settings: Any) -> dict[str, Any]:
    """读取 Gateway 工作负载身份可访问的只读投影，拒绝任何非契约响应。"""
    headers = {
        "X-Tenant-Id": settings.catalog_projection_tenant_id,
        "X-User-Id": "tool-gateway",
        "X-Runtime-Key": settings.control_plane_runtime_api_key,
    }
    response = httpx.get(
        f"{settings.control_plane_base_url.rstrip('/')}/internal/v1/tool-catalog/runtime-projection",
        headers=headers,
        timeout=settings.catalog_projection_timeout_seconds,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("projection_version") != "tool-runtime-projection/v1":
        raise ValueError("unsupported Tool Runtime Projection version")
    catalog = payload.get("catalog")
    if not isinstance(catalog, dict) or not isinstance(catalog.get("tools"), list):
        raise ValueError("Tool Runtime Projection has no catalog payload")
    return catalog
