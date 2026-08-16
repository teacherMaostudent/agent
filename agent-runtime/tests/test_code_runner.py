import pytest
from platform_sdk.tools.registry import ToolContext, ToolRegistry
from pydantic import BaseModel

from agent_runtime_service.runtime.code_runner import (
    CodeRunnerPolicyError,
    CodeRunRequest,
    ControlledCodeRunner,
)


class _Args(BaseModel):
    language: str
    code: str
    timeout_seconds: int
    memory_mb: int


def test_code_runner_only_delegates_to_version_pinned_tool_gateway_contract() -> None:
    """Code Runner 不在 Runtime 本地执行代码，且缺少发布工具版本时必须拒绝。"""
    registry = ToolRegistry()
    registry.register(
        "controlled_code_runner",
        "sandbox",
        _Args,
        lambda args, _: {"status": "ok", "code": args.code},
        {"code:run"},
    )
    runner = ControlledCodeRunner(registry, policy=__import__(
        "agent_runtime_service.runtime.code_runner", fromlist=["SandboxPolicy"]
    ).SandboxPolicy())
    context = ToolContext(tenant_id="t", user_id="u", permissions=frozenset({"code:run"}))
    with pytest.raises(CodeRunnerPolicyError, match="version"):
        runner.execute(CodeRunRequest(code="print('ok')"), context)

    result = runner.execute(
        CodeRunRequest(code="print('ok')"),
        ToolContext(
            tenant_id="t", user_id="u", permissions=frozenset({"code:run"}), tool_version="1.0.0"
        ),
    )
    assert result["status"] == "ok"


def test_code_runner_rejects_host_escape_primitives_before_delegation() -> None:
    """静态预检是远端沙箱之前的额外防线，危险代码不能到达工具调用层。"""
    with pytest.raises(ValueError, match="forbidden"):
        CodeRunRequest(code="import socket")
