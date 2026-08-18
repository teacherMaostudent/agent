"""Context 组装的可审计变换流水线。

Pipeline 只组织模型本轮可见内容的生成顺序，不拥有消息、证据或索引数据；这些数据
仍分别属于 Context Store 和 RAG Service。每一步都接收并返回同一个短生命周期状态，
因而可在不把业务逻辑迁入 Runtime 的前提下扩展策略阶段。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Protocol

from app.contracts.context import ContextAssembleRequest, ContextBudgetReport, ConversationMessage
from app.domain.models import Evidence


@dataclass
class ContextAssemblyState:
    """一次 Context 请求在 Transformer 间流动的受限中间状态。"""

    request: ContextAssembleRequest
    budget: int
    messages: list[ConversationMessage] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)
    rag_status: str = "not_requested"
    degraded: bool = False
    degrade_reason: str | None = None
    budget_report: ContextBudgetReport | None = None


class ContextTransformer(Protocol):
    """Context 阶段的稳定扩展接口；实现不能越权读取其他服务的内部存储。"""

    name: str

    def transform(self, state: ContextAssemblyState) -> ContextAssemblyState:
        """基于当前阶段输入生成下一状态，异常由调用方按其失败策略处理。"""
        ...


class ContextPipeline:
    """按启动期固定顺序执行 Context Transformer，禁止请求期插入未知插件。"""

    def __init__(self, transformers: Iterable[ContextTransformer]) -> None:
        """冻结阶段列表并拒绝同名阶段，确保审计与回放能引用稳定的处理顺序。"""
        stages = tuple(transformers)
        names = [stage.name.strip() for stage in stages]
        if not all(names) or len(names) != len(set(names)):
            raise ValueError("context pipeline transformer names must be unique and non-empty")
        self._transformers = stages

    @property
    def stage_names(self) -> tuple[str, ...]:
        """返回不可变阶段名称，供 Trace 与运行说明关联 Context 选择过程。"""
        return tuple(stage.name for stage in self._transformers)

    def assemble(self, state: ContextAssemblyState) -> ContextAssemblyState:
        """顺序运行全部阶段；任何必需阶段失败都不能被后续阶段静默掩盖。"""
        for transformer in self._transformers:
            state = transformer.transform(state)
        return state
