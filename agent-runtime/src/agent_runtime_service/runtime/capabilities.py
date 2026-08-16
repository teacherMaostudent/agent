"""Runtime 启动期冻结的外围能力目录。"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from typing import Any

from platform_sdk.contracts.capabilities import (
    CapabilityManifest,
    RuntimeCapability,
    capability_manifest_digest,
)


class CapabilityUnavailable(RuntimeError):
    """发布计划需要的能力未在当前 Runtime 集群部署时抛出。"""


class CapabilityRegistry:
    """保存启动期装配的能力 Provider，禁止请求路径动态注册或替换。"""

    def __init__(
        self,
        providers: Mapping[RuntimeCapability | str, Any],
        *,
        version: str,
        manifests: Iterable[CapabilityManifest] | None = None,
    ) -> None:
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
        supplied = {item.capability.value: item for item in manifests or ()}
        if set(supplied) - set(normalized):
            raise ValueError("capability manifest declares an undeployed provider")
        self._manifests = tuple(
            supplied.get(
                name,
                CapabilityManifest(
                    capability=RuntimeCapability(name),
                    provider_id=f"runtime.{name}",
                    provider_version=version,
                    artifact_digest=hashlib.sha256(
                        f"{name}:{type(provider).__module__}.{type(provider).__qualname__}:{version}".encode()
                    ).hexdigest(),
                ),
            )
            for name, provider in sorted(normalized.items())
        )
        self._manifest_digest = capability_manifest_digest(self._manifests)

    @property
    def version(self) -> str:
        """返回可审计的静态目录版本，不暴露任何可变注册接口。"""
        return self._version

    @property
    def names(self) -> tuple[str, ...]:
        """返回本实例实际已装配的能力名称，供 Control Plane 远程证明。"""
        return tuple(sorted(self._providers))

    @property
    def manifests(self) -> tuple[CapabilityManifest, ...]:
        """返回不可变能力声明，供部署 Catalog 进行实例事实校验。"""
        return self._manifests

    @property
    def manifest_digest(self) -> str:
        """返回冻结清单摘要；同一版本目录的 Provider 漂移也会改变此值。"""
        return self._manifest_digest

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

    def validate_profiles(self, required: Iterable[str], executor_profile: str) -> None:
        """验证计划能力不仅存在，还明确声明可供目标执行器 Profile 使用。"""
        self.validate(required)
        incompatible = [
            item.capability.value
            for item in self._manifests
            if item.capability.value in set(required)
            and item.executor_profiles
            and executor_profile not in item.executor_profiles
        ]
        if incompatible:
            raise CapabilityUnavailable(
                "runtime capability does not support executor profile "
                f"'{executor_profile}': {', '.join(sorted(incompatible))}"
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
