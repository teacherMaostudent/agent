"""执行器可见的强类型 Runtime 能力集合。

该模块把启动期已冻结的服务客户端收敛为一个运行上下文。执行器只依赖这里声明的
能力协议，不读取容器、环境变量或通用 ``Map[str, object]``，从而避免将基础设施
装配细节泄漏到 Graph、Planner 或业务 Agent。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from platform_sdk.contracts.context import ContextAssembleRequest
from platform_sdk.contracts.rag import RagSearchRequest
from platform_sdk.tools.registry import ToolContext


class ContextCapability(Protocol):
    """提供受 ACL 保护的消息写入与上下文组装，原始历史不属于 Runtime。"""

    def assemble(self, request: ContextAssembleRequest, **kwargs: Any) -> Any:
        """根据稳定 Context 契约返回模型可见的历史和证据包。"""
        ...

    def append_message(self, *args: Any, **kwargs: Any) -> Any:
        """将外部输入写入 Context 数据域，供后续安全点重新组装。"""
        ...


class RetrievalCapability(Protocol):
    """提供经 RAG ACL、索引版本和证据契约约束的检索能力。"""

    def search(self, request: RagSearchRequest) -> Any:
        """返回可引用证据；实现不得向执行器暴露索引内部对象。"""
        ...


class ToolCapability(Protocol):
    """提供受 Tool Gateway 目录、授权、审批和幂等约束的工具入口。"""

    def execute(self, tool_name: str, arguments: dict[str, Any], context: ToolContext) -> Any:
        """执行已发布工具版本；Gateway 仍是最终授权和副作用边界。"""
        ...

    def manifests(self, *args: Any, **kwargs: Any) -> Any:
        """返回当前调用主体可见的工具目录投影，供模型动作受限选择。"""
        ...


class LlmCapability(Protocol):
    """提供经过 Gateway 路由、预算、限流和模型版本治理的模型调用能力。"""

    def complete_json(self, *args: Any, **kwargs: Any) -> Any:
        """请求一次受治理的 JSON 生成；Provider 选择不属于执行器。"""
        ...


@dataclass(frozen=True)
class RuntimeContext:
    """一次 Runtime 实例启动后不可替换的强类型能力视图。

    可选能力只允许在启动期为空，发布计划验证会在 Run 开始前拒绝需要而未部署的
    组合；请求或模型输出不能动态添加客户端、网络地址或本地插件。
    """

    context: ContextCapability
    tools: ToolCapability
    llm: LlmCapability | None = None
    retrieval: RetrievalCapability | None = None
    session: Any | None = None
    workflow: Any | None = None
    agents: Any | None = None

    def require_retrieval(self) -> RetrievalCapability:
        """取得检索能力；未部署时显式失败，避免把知识请求静默降级为空结果。"""
        if self.retrieval is None:
            raise RuntimeError("retrieval capability is not deployed")
        return self.retrieval

    def require_llm(self) -> LlmCapability:
        """取得模型能力；离线测试必须显式使用 Offline Decision Engine。"""
        if self.llm is None:
            raise RuntimeError("llm capability is not deployed")
        return self.llm
