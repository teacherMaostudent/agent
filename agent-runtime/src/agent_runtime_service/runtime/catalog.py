"""Runtime 集群已部署执行器的只读目录。

目录在进程启动期间由 Container 装配；请求处理阶段只能解析已部署的 Profile，
不能依据发布快照动态加载业务代码或注册新的执行器。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from platform_sdk.contracts.execution_profile import (
    PROFILE_BY_EXECUTION,
    REQUIREMENTS_BY_PROFILE,
    DurabilityStrategy,
    ExecutionEngine,
    legacy_execution_mode,
)

from agent_runtime_service.runtime.harness import ExecutorAdapter
from agent_runtime_service.runtime.models import (
    ExecutionLifecycle,
    ExecutionMode,
    ExecutionRequirements,
    ReasoningMode,
)


@dataclass(frozen=True)
class ExecutionProvider:
    """一个启动期部署的执行 Provider 及其可公开验证的执行边界。"""

    profile: str
    mode: ExecutionMode
    executor: ExecutorAdapter
    supports_resume: bool = True
    lifecycle: ExecutionLifecycle = ExecutionLifecycle.REQUEST_SCOPED
    reasoning: ReasoningMode = ReasoningMode.GRAPH
    engine: ExecutionEngine | None = None
    durability: DurabilityStrategy | None = None

    @property
    def requirements(self) -> ExecutionRequirements:
        """返回该部署 Profile 明确承诺的双轴能力，而非从名称猜测语义。"""
        return ExecutionRequirements(
            lifecycle=self.lifecycle,
            reasoning=self.reasoning,
            engine=self.engine,
            durability=self.durability,
        )

    @property
    def normalized_engine(self) -> ExecutionEngine:
        """兼容旧 Provider 声明，并投影为唯一执行内核。"""
        return self.requirements.normalized_engine()

    @property
    def normalized_durability(self) -> DurabilityStrategy:
        """兼容旧 Provider 声明，并投影为唯一持久化策略。"""
        return self.requirements.normalized_durability()


class ExecutionProviderRegistry:
    """冻结执行 Provider 的启动目录，发布快照只能解析其中已部署的 Profile。"""

    def __init__(self, providers: Mapping[str, ExecutionProvider]) -> None:
        """复制并校验 Provider，避免请求期注册业务执行代码或覆盖既有 Profile。"""
        self._providers: dict[str, ExecutionProvider] = {}
        for profile, provider in providers.items():
            key = profile.strip()
            if not key or key != provider.profile.strip() or key in self._providers:
                raise ValueError(f"invalid or duplicate execution provider profile: {profile}")
            self._providers[key] = provider

    @property
    def profiles(self) -> tuple[str, ...]:
        """返回稳定排序的已部署 Profile，供发布前与运行时能力校验读取。"""
        return tuple(sorted(self._providers))

    @property
    def providers(self) -> tuple[ExecutionProvider, ...]:
        """返回去除执行器内部对象后的 Provider 元数据排序视图。"""
        return tuple(self._providers[name] for name in self.profiles)

    def provider(self, profile: str) -> ExecutionProvider:
        """解析完整 Provider；未知 Profile 必须在执行任何业务步骤前失败。"""
        key = profile.strip()
        try:
            return self._providers[key]
        except KeyError as exc:
            raise LookupError(f"executor profile is not deployed: {key or '<empty>'}") from exc

    def resolve(self, profile: str) -> ExecutorAdapter:
        """保持 Harness 的窄执行器接口，同时由 Registry 承担 Profile 实例校验。"""
        return self.provider(profile).executor

    def resolve_requirements(self, requirements: ExecutionRequirements) -> ExecutorAdapter:
        """按执行内核与持久化策略解析部署组合，找不到或歧义时均失败关闭。"""
        preferred_profile = PROFILE_BY_EXECUTION.get(
            (
                requirements.normalized_engine(),
                requirements.normalized_durability(),
            )
        )
        if preferred_profile in self._providers:
            return self._providers[preferred_profile].executor
        matches = [
            provider
            for provider in self._providers.values()
            if provider.normalized_engine == requirements.normalized_engine()
            and provider.normalized_durability == requirements.normalized_durability()
        ]
        if len(matches) != 1:
            rendered = (
                f"{requirements.normalized_engine().value}/"
                f"{requirements.normalized_durability().value}"
            )
            raise LookupError(f"execution requirements are not uniquely deployed: {rendered}")
        return matches[0].executor


class ExecutorCatalog(ExecutionProviderRegistry):
    """保存当前 Runtime 集群允许执行的 Profile 与执行器映射。"""

    def __init__(self, entries: Mapping[str, ExecutorAdapter]) -> None:
        """冻结启动期装配的执行器目录，拒绝空 Profile 与重复归一化键。"""
        super().__init__(
            {
                profile: ExecutionProvider(
                    profile=profile,
                    mode=_mode_for_profile(profile),
                    executor=executor,
                    supports_resume=profile != "simple/v1",
                    lifecycle=_requirements_for_profile(profile).lifecycle,
                    reasoning=_requirements_for_profile(profile).reasoning,
                    engine=_requirements_for_profile(profile).normalized_engine(),
                    durability=_requirements_for_profile(profile).normalized_durability(),
                )
                for profile, executor in entries.items()
            }
        )


def _mode_for_profile(profile: str) -> ExecutionMode:
    """为历史 Profile 推导稳定执行模式，兼容既有发布快照而不模糊长期边界。"""
    return ExecutionMode(legacy_execution_mode(profile.strip()))


def _requirements_for_profile(profile: str) -> ExecutionRequirements:
    """为历史部署 Profile 提供显式双轴映射，作为迁移期唯一兼容入口。"""
    shared = REQUIREMENTS_BY_PROFILE.get(profile.strip())
    if shared is None:
        return ExecutionRequirements(
            lifecycle=ExecutionLifecycle.REQUEST_SCOPED,
            reasoning=ReasoningMode.GRAPH,
        )
    return ExecutionRequirements(
        lifecycle=ExecutionLifecycle(shared.lifecycle),
        reasoning=ReasoningMode(shared.reasoning),
    )
