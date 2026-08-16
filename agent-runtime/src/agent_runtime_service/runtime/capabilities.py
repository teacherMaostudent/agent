"""Runtime 启动期冻结的外围能力目录。"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from platform_sdk.contracts.capabilities import RuntimeCapability


class CapabilityUnavailable(RuntimeError):
    """发布计划需要的能力未在当前 Runtime 集群部署时抛出。"""


class CapabilityRegistry:
    """保存启动期装配的能力 Provider，禁止请求路径动态注册或替换。"""

    def __init__(self, providers: Mapping[RuntimeCapability | str, Any], *, version: str) -> None:
        """复制并校验 Provider 映射，使容器外部无法修改已公布的能力目录。"""
        if not version.strip():
            raise ValueError("capability catalog version must not be empty")
        normalized: dict[str, Any] = {}
        for raw_name, provider in providers.items():
            name = str(raw_name)
            if name not in {item.value for item in RuntimeCapability}:
                raise ValueError(f"unknown runtime capability: {name}")
            if provider is None:
                raise ValueError(f"runtime capability provider is missing: {name}")
            if name in normalized:
                raise ValueError(f"runtime capability is registered twice: {name}")
            normalized[name] = provider
        self._providers = normalized
        self._version = version

    @property
    def version(self) -> str:
        """返回可审计的静态目录版本，不暴露任何可变注册接口。"""
        return self._version

    @property
    def names(self) -> tuple[str, ...]:
        """返回本实例实际已装配的能力名称，供 Control Plane 远程证明。"""
        return tuple(sorted(self._providers))

    def require(self, capability: RuntimeCapability | str) -> Any:
        """取得已部署 Provider；缺失时 fail-closed，禁止请求路径隐式降级。"""
        name = str(capability)
        try:
            return self._providers[name]
        except KeyError as exc:
            raise CapabilityUnavailable(f"runtime capability is unavailable: {name}") from exc

    def validate(self, required: Iterable[str]) -> None:
        """在调用执行器前验证发布计划的全部能力，避免半执行后才暴露部署缺口。"""
        missing = sorted({str(item) for item in required} - set(self._providers))
        if missing:
            raise CapabilityUnavailable(
                "published plan requires undeployed runtime capabilities: " + ", ".join(missing)
            )

    def close(self) -> None:
        """关闭每个唯一 Provider；同一客户端即使映射多次也只能关闭一次。"""
        closed: set[int] = set()
        for provider in self._providers.values():
            if id(provider) in closed:
                continue
            closed.add(id(provider))
            closer = getattr(provider, "close", None)
            if callable(closer):
                closer()
