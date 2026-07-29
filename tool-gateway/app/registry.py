from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from pydantic import ValidationError

from app.domain.errors import ToolDisabledError, ToolNotFoundError
from app.domain.models import ToolCatalog, ToolManifest, ToolSpec
from app.infrastructure.adapters import McpToolAdapter, ToolAdapter, build_http_adapter


class ToolRegistry:
    """Immutable startup registry keyed by logical tool name and version."""

    def __init__(self) -> None:
        self._specs: dict[tuple[str, str], ToolSpec] = {}
        self._adapters: dict[tuple[str, str], ToolAdapter] = {}
        self._versions: dict[str, list[str]] = defaultdict(list)

    def register(self, spec: ToolSpec, adapter: ToolAdapter) -> None:
        if spec.key in self._specs:
            raise ValueError(f"tool already registered: {spec.name}:{spec.version}")
        Draft202012Validator.check_schema(spec.input_schema)
        if spec.output_schema is not None:
            Draft202012Validator.check_schema(spec.output_schema)
        self._specs[spec.key] = spec
        self._adapters[spec.key] = adapter
        self._versions[spec.name].append(spec.version)

    def resolve(self, name: str, version: str | None = None) -> tuple[ToolSpec, ToolAdapter]:
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
        if not spec.is_enabled_for(tenant_id):
            raise ToolDisabledError(f"unknown tool: {spec.name}")

    @property
    def count(self) -> int:
        return len(self._specs)

    def adapters(self) -> list[ToolAdapter]:
        return list({id(adapter): adapter for adapter in self._adapters.values()}.values())


def load_registry(
    path: Path,
    *,
    allow_private_networks: bool,
    max_response_bytes: int,
) -> ToolRegistry:
    try:
        raw: Any = json.loads(path.read_text(encoding="utf-8"))
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
                allow_private_networks=allow_private_networks,
                max_response_bytes=max_response_bytes,
            )
        else:
            adapter = McpToolAdapter(
                transport,
                allow_private_networks=allow_private_networks,
            )
        registry.register(spec, adapter)
    return registry
