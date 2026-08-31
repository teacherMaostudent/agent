"""桌面交互提交入口的契约测试。"""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from platform_sdk.contracts.capabilities import RuntimeCapability
from platform_sdk.contracts.runtime_api import AgentRunRequest

from agent_runtime_service.runtime.integration import ReleaseNotFoundError
from agent_runtime_service.service_api.runtime_api import (
    _effective_attempt_budget,
    submit_interactive_agent_run,
)


class _Queue:
    """记录冻结提交，模拟本地队列或 Temporal Provider。"""

    def __init__(self) -> None:
        self.submission = None

    def submit(self, submission):
        """保存提交并返回预分配运行标识。"""
        self.submission = submission
        return {"run_id": "run-desktop", "status": "QUEUED", "result": {}}


class _Harness:
    """只实现交互入口所需的发布解析与快照加载边界。"""

    def resolve_release(self, **kwargs):
        """返回提交期冻结的发布解析结果。"""
        return {"version_id": "version-1", "snapshot": {"agent_id": kwargs["agent_id"]}}

    def load_snapshot(self, resolution, *, tenant_id, agent_id):
        """返回带数据区域的已编译计划替身。"""
        del resolution, tenant_id, agent_id
        return SimpleNamespace(plan=SimpleNamespace(data_region="cn-east"))


def test_interactive_submit_freezes_release_and_returns_run_id() -> None:
    """交互任务必须先冻结发布，再进入唯一异步执行路径。"""
    queue = _Queue()
    container = SimpleNamespace(
        settings=SimpleNamespace(oidc_enabled=False),
        agent_harness=_Harness(),
        capability=lambda capability: queue
        if capability == RuntimeCapability.WORKFLOW
        else None,
    )
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(container=container)), scope={}
    )

    result = submit_interactive_agent_run(
        AgentRunRequest(task="扫描工作区", agent_id="desktop-agent"),
        request,
        x_tenant_id="tenant-a",
        x_user_id="user-a",
        x_permissions="rag:read,file:scan",
        x_request_id="request-a",
        x_trace_id="trace-a",
    )

    assert result["run_id"] == "run-desktop"
    assert queue.submission["release_resolution"]["version_id"] == "version-1"
    assert queue.submission["interaction_channel"] == "desktop"
    assert queue.submission["permissions"] == "file:scan,rag:read"


def test_interactive_submit_reports_missing_release_as_404() -> None:
    """未发布 Agent 属于可修复配置错误，桌面端不应只看到无意义的 Runtime 500。"""

    class MissingReleaseHarness(_Harness):
        def resolve_release(self, **kwargs):
            """模拟 Control Plane 对当前租户和环境找不到 Active Release。"""
            del kwargs
            raise ReleaseNotFoundError("No active release exists for this Agent.")

    container = SimpleNamespace(
        settings=SimpleNamespace(oidc_enabled=False),
        agent_harness=MissingReleaseHarness(),
    )
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(container=container)), scope={}
    )

    with pytest.raises(HTTPException) as captured:
        submit_interactive_agent_run(
            AgentRunRequest(task="测试", agent_id="missing-agent"),
            request,
            x_tenant_id="tenant-a",
            x_user_id="user-a",
            x_permissions="rag:read",
            x_request_id="request-a",
            x_trace_id="trace-a",
        )

    assert captured.value.status_code == 404
    assert captured.value.detail["code"] == "agent_release_not_found"


def test_default_attempt_budget_covers_published_action_budgets() -> None:
    """默认总尝试预算不得早于 LLM、工具和检索三个已发布子预算耗尽。"""
    settings = SimpleNamespace(
        agent_attempt_budget=6,
        agent_max_llm_calls=8,
        agent_max_tool_calls=6,
        agent_max_retrieval_rounds=4,
    )
    limits = {"max_llm_calls": 4, "max_tool_calls": 3, "max_retrieval_rounds": 3}
    assert _effective_attempt_budget(AgentRunRequest(task="scan"), limits, settings) == 10
    assert (
        _effective_attempt_budget(
            AgentRunRequest(task="scan", attempt_budget=3), limits, settings
        )
        == 3
    )
