"""显式绑定到 Tool Gateway 的隔离 Code Runner 能力。

Runtime 从不在自身进程执行模型生成的代码。该能力仅把通过静态预检的请求委派给
版本固定的 ``controlled_code_runner`` 工具；真实的容器、网络、文件挂载、资源配额
和镜像签名必须由 Tool Gateway 后端的 Sandbox Provider 强制执行。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from platform_sdk.tools.registry import ToolContext, ToolRegistry
from pydantic import BaseModel, Field, field_validator


class CodeRunnerPolicyError(RuntimeError):
    """代码执行请求不满足发布边界或基础安全预检时抛出。"""


class CodeRunRequest(BaseModel):
    """受限 Code Runner 的输入契约；不提供宿主路径、网络地址或任意命令入口。"""

    language: str = Field(default="python", pattern=r"^python$")
    code: str = Field(min_length=1, max_length=24_000)
    timeout_seconds: int = Field(default=30, ge=1, le=120)
    memory_mb: int = Field(default=256, ge=64, le=1024)

    @field_validator("code")
    @classmethod
    def reject_obvious_escape_attempts(cls, value: str) -> str:
        """拒绝显而易见的宿主逃逸意图；这只是前置防线，不能替代隔离后端。"""
        forbidden = (r"\bsubprocess\b", r"\bos\.system\b", r"\bsocket\b", r"\bpty\b")
        if any(re.search(pattern, value, flags=re.IGNORECASE) for pattern in forbidden):
            raise ValueError("code requests a forbidden host or network primitive")
        return value


@dataclass(frozen=True)
class SandboxPolicy:
    """Runtime 可验证的沙箱声明；实际隔离由远端受控工具服务执行。"""

    provider_id: str = "tool-gateway-controlled-code-runner"
    network_egress: bool = False
    writable_workspace: bool = False
    max_timeout_seconds: int = 120
    max_memory_mb: int = 1024


class ControlledCodeRunner:
    """通过 Tool Gateway 调度已发布的隔离代码任务，不提供本地 ``exec`` 回退。"""

    tool_name = "controlled_code_runner"

    def __init__(self, tool_registry: ToolRegistry, policy: SandboxPolicy) -> None:
        """保存受控工具客户端和不可变沙箱声明，避免模型自行选择执行后端。"""
        self._tools = tool_registry
        self._policy = policy

    def execute(self, request: CodeRunRequest, context: ToolContext) -> object:
        """校验资源上限后调用固定工具名；Snapshot 必须同时绑定该工具的精确版本。"""
        if request.timeout_seconds > self._policy.max_timeout_seconds:
            raise CodeRunnerPolicyError("code timeout exceeds sandbox policy")
        if request.memory_mb > self._policy.max_memory_mb:
            raise CodeRunnerPolicyError("code memory exceeds sandbox policy")
        if context.tool_version == "":
            raise CodeRunnerPolicyError("published code runner tool version is required")
        return self._tools.execute(
            self.tool_name,
            request.model_dump(mode="json"),
            context,
        )
