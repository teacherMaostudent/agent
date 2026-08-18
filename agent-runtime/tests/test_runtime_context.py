"""强类型执行上下文的边界测试。"""

from __future__ import annotations

import pytest
from platform_sdk.tools.registry import ToolRegistry

from agent_runtime_service.runtime.runtime_context import RuntimeContext


class ContextDouble:
    """最小 Context 能力替身，证明 RuntimeContext 不依赖容器或通用目录。"""

    def assemble(self, request, **kwargs):
        """测试不需要真正组装上下文，因此只验证方法形状。"""
        del request, kwargs
        return None

    def append_message(self, *args, **kwargs):
        """测试不需要写入正文，因此只验证能力边界。"""
        del args, kwargs
        return None


def test_runtime_context_exposes_only_declared_typed_capabilities() -> None:
    """执行器可取得 Context/Tool，却不能把缺失 LLM/RAG 静默降级为空对象。"""
    runtime = RuntimeContext(context=ContextDouble(), tools=ToolRegistry())

    assert runtime.context is not None
    assert runtime.tools is not None
    with pytest.raises(RuntimeError, match="llm capability"):
        runtime.require_llm()
    with pytest.raises(RuntimeError, match="retrieval capability"):
        runtime.require_retrieval()
