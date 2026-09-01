"""冻结 Snapshot 中的工具能力解析器。"""

from __future__ import annotations

from typing import Any

from agent_runtime_service.runtime.models import RuntimeLimitExceeded


class ToolCapabilityEvaluator:
    """将模型提出的工具名约束为已发布、精确版本的能力绑定。"""

    @staticmethod
    def resolve(state: dict[str, Any], tool_name: str) -> dict[str, Any]:
        """只读取编译计划或同一不可变 Snapshot，永不查询活动目录。"""
        bindings = state.get("compiled_plan", {}).get("tools", [])
        if not bindings:
            bindings = state.get("agent_snapshot", {}).get("spec", {}).get("tools", [])
        binding = next(
            (item for item in bindings if isinstance(item, dict) and item.get("tool_name") == tool_name),
            None,
        )
        if binding is None:
            raise RuntimeLimitExceeded(
                "TOOL_NOT_PUBLISHED",
                f"Tool '{tool_name}' is not bound in the published Agent snapshot.",
            )
        if not str(binding.get("version", "")).strip():
            raise RuntimeLimitExceeded(
                "TOOL_VERSION_MISSING", f"Tool '{tool_name}' has no frozen version in the snapshot."
            )
        return dict(binding)

    @classmethod
    def version(cls, state: dict[str, Any], tool_name: str) -> str:
        """返回被能力评估器确认后的精确工具版本。"""
        return str(cls.resolve(state, tool_name)["version"])
