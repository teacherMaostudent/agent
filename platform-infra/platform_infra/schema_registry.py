"""Versioned JSON Schema registry shared by Python services and CI.

Schemas are immutable files in ``platform-contracts/schemas``.  Services use
this small adapter for fail-closed validation; a managed registry can replace
the file store later without changing callers.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator


class SchemaRegistry:
    """Fail-closed reader for versioned contracts shared across services.

    The on-disk implementation keeps local development simple.  Callers use
    this abstraction so it can later be backed by a managed registry without
    changing the validation boundary in each service.
    """
    def __init__(self, schema_dir: str | Path) -> None:
        """固定只读 Schema 目录并建立进程内缓存；版本由文件名/发布制品决定。"""
        self.schema_dir = Path(schema_dir)
        self._schemas: dict[str, dict[str, Any]] = {}

    def get(self, name: str) -> dict[str, Any]:
        """加载并校验一个 Schema；损坏、缺失或非法定义都会失败关闭。"""
        if name not in self._schemas:
            path = self.schema_dir / name
            try:
                schema = json.loads(path.read_text(encoding="utf-8"))
                Draft202012Validator.check_schema(schema)
            except (OSError, json.JSONDecodeError, ValueError) as exc:
                raise ValueError(f"invalid contract schema: {path}") from exc
            self._schemas[name] = schema
        return self._schemas[name]

    def validate(self, name: str, payload: Any) -> None:
        """验证 Payload 并报告首个稳定排序错误位置，供生产者修复契约违例。"""
        errors = sorted(
            Draft202012Validator(self.get(name)).iter_errors(payload),
            key=lambda item: list(item.path),
        )
        if errors:
            error = errors[0]
            location = ".".join(str(item) for item in error.path) or "$"
            raise ValueError(f"schema validation failed for {name} at {location}: {error.message}")
