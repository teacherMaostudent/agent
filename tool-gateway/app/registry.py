"""Versioned tool catalog boundary.

Only catalogued versions can be resolved.  Runtime plans therefore cannot
escalate privileges by naming an adapter that was not published for the tenant.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from platform_infra.schema_registry import SchemaRegistry
from pydantic import ValidationError

from app.domain.errors import ToolDisabledError, ToolNotFoundError
from app.domain.models import ToolCatalog, ToolManifest, ToolSpec
from app.infrastructure.adapters import McpToolAdapter, ToolAdapter, build_http_adapter


class ToolRegistry:
    """Immutable startup registry keyed by logical tool name and version.

    Tool definitions are loaded once from the signed/deployed catalog rather
    than accepted from an LLM request.  Selecting the latest version is only a
    convenience for callers; release snapshots should always pin a version.
    """

    def __init__(self) -> None:
        """初始化仅允许启动期写入的版本化规格、适配器和版本索引。"""
        self._specs: dict[tuple[str, str], ToolSpec] = {}
        self._adapters: dict[tuple[str, str], ToolAdapter] = {}
        self._versions: dict[str, list[str]] = defaultdict(list)

    def register(self, spec: ToolSpec, adapter: ToolAdapter) -> None:
        """注册一个固定名称和版本的工具及适配器；重复键或清单不一致时拒绝覆盖。

        Register one immutable catalog version; duplicates are configuration errors.
        """
        if spec.key in self._specs:
            raise ValueError(f"tool already registered: {spec.name}:{spec.version}")
        Draft202012Validator.check_schema(spec.input_schema)
        if spec.output_schema is not None:
            Draft202012Validator.check_schema(spec.output_schema)
        self._specs[spec.key] = spec
        self._adapters[spec.key] = adapter
        self._versions[spec.name].append(spec.version)

    def resolve(self, name: str, version: str | None = None) -> tuple[ToolSpec, ToolAdapter]:
        """按精确名称和版本解析工具，不允许回退到最新版本，避免发布快照发生隐式漂移。

        Resolve an explicit or latest published version without bypassing the catalog.
        """
        if version is None:
            versions = self._versions.get(name, [])
            if not versions:
                raise ToolNotFoundError(f"unknown tool: {name}")
            version = versions[-1]
        key = (name, version)
        spec = self._specs.get(key)
        adapter = self._adapters.get(key)
        if spec is None or adapter is None:
            raise ToolNotFoundError(f"unknown tool version: {name}:{version}")
        return spec, adapter

    def manifests(
        self,
        tenant_id: str,
        permissions: frozenset[str],
    ) -> list[ToolManifest]:
        """返回调用租户可见的工具清单投影，不暴露适配器凭据或内部端点。

        Expose only the latest tenant-visible version whose permissions the caller owns.
        """
        manifests: list[ToolManifest] = []
        for name in sorted(self._versions):
            spec = next(
                (
                    self._specs[(name, version)]
                    for version in reversed(self._versions[name])
                    if self._specs[(name, version)].is_enabled_for(tenant_id)
                ),
                None,
            )
            if spec is None:
                continue
            if not set(spec.required_permissions).issubset(permissions):
                continue
            manifests.append(ToolManifest.from_spec(spec))
        return manifests

    def assert_visible(self, spec: ToolSpec, tenant_id: str) -> None:
        """确认工具版本对当前租户可见；租户不在允许列表时在解析适配器前失败关闭。

        Enforce tenant allow-lists before an adapter observes invocation input.
        """
        if not spec.is_enabled_for(tenant_id):
            raise ToolDisabledError(f"unknown tool: {spec.name}")

    @property
    def count(self) -> int:
        """返回已注册工具版本数量，用于就绪检查而不触发下游调用。

        Return the number of immutable catalog entries for readiness diagnostics.
        """
        return len(self._specs)

    def adapters(self) -> list[ToolAdapter]:
        """返回生命周期管理所需的适配器集合；调用方只能用于统一关闭，不用于绕过目录执行。

        Return de-duplicated adapter instances because multiple versions may share one client.
        """
        return list({id(adapter): adapter for adapter in self._adapters.values()}.values())


def load_registry(
    path: Path,
    *,
    allow_private_networks: bool,
    max_response_bytes: int,
    client_options: dict[str, Any] | None = None,
    schema_dir: Path | None = None,
) -> ToolRegistry:
    """从版本化 Tool Catalog
    构建只读目录和适配器；任一清单或传输配置无效都会阻止服务就绪。

    Load and schema-validate the deployed catalog before creating any network adapter.
    """
    try:
        raw: Any = json.loads(path.read_text(encoding="utf-8"))
        if schema_dir is not None:
            SchemaRegistry(schema_dir).validate("tool-catalog.v1.json", raw)
        catalog = ToolCatalog.model_validate(raw)
    except FileNotFoundError as exc:
        raise RuntimeError(f"tool catalog does not exist: {path}") from exc
    except (json.JSONDecodeError, ValidationError, ValueError) as exc:
        raise RuntimeError(f"invalid tool catalog {path}: {exc}") from exc

    registry = ToolRegistry()
    for spec in catalog.tools:
        transport = spec.transport
        if transport.kind == "http":
            adapter = build_http_adapter(
                transport,
                allow_private_networks=(
                    allow_private_networks and transport.allow_private_networks
                ),
                max_response_bytes=max_response_bytes,
                client_options=client_options,
            )
        else:
            adapter = McpToolAdapter(
                transport,
                allow_private_networks=(
                    allow_private_networks and transport.allow_private_networks
                ),
            )
        registry.register(spec, adapter)
    return registry
