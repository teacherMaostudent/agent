"""Tool Gateway 的最终策略判定器。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from app.domain.errors import ToolPermissionError
from app.domain.models import InvocationContext, ToolSpec
from app.registry import ToolRegistry


class ToolPolicyEvaluator:
    """在 Adapter 之前统一执行租户、权限、运行身份和外部策略判定。"""

    def __init__(self, registry: ToolRegistry, policy_authorizer: Any = None) -> None:
        self._registry = registry
        self._policy_authorizer = policy_authorizer

    async def evaluate(
        self,
        spec: ToolSpec,
        context: InvocationContext,
        arguments: dict[str, Any],
        *,
        validate_input: Callable[[dict[str, Any] | None, Any], None],
    ) -> None:
        """拒绝未通过门禁的调用；成功返回不代表已经执行 Adapter。"""
        self._registry.assert_visible(spec, context.tenant_id)
        self._authorize_permissions(spec, context)
        self._validate_runtime_action_identity(spec, context)
        if self._policy_authorizer is not None:
            await asyncio.to_thread(
                self._policy_authorizer.authorize,
                {
                    "subject": {
                        "tenant_id": context.tenant_id,
                        "user_id": context.user_id,
                        "permissions": sorted(context.permissions),
                    },
                    "resource": {
                        "type": "tool",
                        "name": spec.name,
                        "version": spec.version,
                        "risk": spec.risk.value,
                    },
                    "action": "execute",
                    "execution": {
                        "operation_id": context.operation_id,
                        "step_id": context.step_id,
                        "plan_id": context.plan_id,
                        "plan_admission_id": context.plan_admission_id,
                        "snapshot_id": context.snapshot_id,
                    },
                },
            )
        validate_input(spec.input_schema, arguments)

    @staticmethod
    def _authorize_permissions(spec: ToolSpec, context: InvocationContext) -> None:
        missing = sorted(set(spec.required_permissions) - context.permissions)
        if missing:
            raise ToolPermissionError(
                f"missing permissions for tool {spec.name}: {', '.join(missing)}"
            )

    @staticmethod
    def _validate_runtime_action_identity(spec: ToolSpec, context: InvocationContext) -> None:
        """写操作必须携带冻结 Snapshot 与准入计划中的唯一操作身份。"""
        if spec.risk.value == "read_only" or not context.snapshot_id:
            return
        required = {
            "operation_id": context.operation_id,
            "step_id": context.step_id,
            "plan_id": context.plan_id,
            "plan_admission_id": context.plan_admission_id,
        }
        missing = sorted(name for name, value in required.items() if not value.strip())
        if missing:
            raise ToolPermissionError(
                "runtime write action is missing admitted operation identity: " + ", ".join(missing)
            )
