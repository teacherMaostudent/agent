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
        """处理 register 对应的当前组件内部业务步骤。


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
        """处理 resolve 对应的当前组件内部业务步骤。


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
        """处理 manifests 对应的当前组件内部业务步骤。


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
        """校验 assert_visible 对应的受控业务步骤。


        Enforce tenant allow-lists before an adapter observes invocation input.
        """
        if not spec.is_enabled_for(tenant_id):
            raise ToolDisabledError(f"unknown tool: {spec.name}")

    @property
    def count(self) -> int:
        """处理 count 对应的当前组件内部业务步骤。


        Return the number of immutable catalog entries for readiness diagnostics.
        """
        return len(self._specs)

    def adapters(self) -> list[ToolAdapter]:
        """处理 adapters 对应的当前组件内部业务步骤。


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
    """读取或查询 load_registry 对应的受控业务步骤。


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
