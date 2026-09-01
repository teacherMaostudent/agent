"""Runtime 工具提议授权链的显式组件回归。"""

from __future__ import annotations

import pytest

from agent_runtime_service.runtime.capability_evaluator import ToolCapabilityEvaluator
from agent_runtime_service.runtime.models import RuntimeLimitExceeded
from agent_runtime_service.runtime.reference_monitor import RuntimeReferenceMonitor
from agent_runtime_service.runtime.tool_execution import SideEffectBarrier, ToolExecutionPolicy


def test_capability_evaluator_requires_a_versioned_snapshot_binding() -> None:
    """模型提出的名称不能绕过 Snapshot 直接解析活动工具目录。"""
    state = {"agent_snapshot": {"spec": {"tools": [{"tool_name": "files.scan", "version": "1"}]}}}

    assert ToolCapabilityEvaluator.version(state, "files.scan") == "1"
    with pytest.raises(RuntimeLimitExceeded) as exc_info:
        ToolCapabilityEvaluator.resolve(state, "files.delete")
    assert exc_info.value.code == "TOOL_NOT_PUBLISHED"


def test_reference_monitor_rejects_outside_admitted_tool_scope() -> None:
    """即使工具已发布，只要本次准入计划未允许，也不能进入 Gateway。"""
    monitor = RuntimeReferenceMonitor(SideEffectBarrier())
    state = {
        "compiled_plan": {"contract_hash": "plan-hash"},
        "plan_admission": {"allowed_tool_scope": ["files.scan"]},
        "tenant_id": "tenant-a",
        "run_id": "run-a",
        "snapshot_id": "snapshot-a",
    }
    policy = ToolExecutionPolicy.from_published_binding(
        {"risk": "read_only", "idempotent": True}, tenant_id="tenant-a", tool_name="files.delete"
    )

    with pytest.raises(RuntimeLimitExceeded) as exc_info:
        monitor.evaluate(
            state,
            tool_name="files.delete",
            binding={"tool_name": "files.delete", "version": "1"},
            policy=policy,
            tool_execution_id="tool-execution-1",
        )
    assert exc_info.value.code == "TOOL_OUTSIDE_ADMITTED_SCOPE"
