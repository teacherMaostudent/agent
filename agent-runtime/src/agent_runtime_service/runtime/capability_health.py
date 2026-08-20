"""统一 Capability Provider 语义健康投影。"""

from datetime import UTC, datetime

from platform_sdk.contracts.skills import (
    CapabilityProviderDescriptor,
    ProviderHealthStatus,
)
from pydantic import BaseModel, Field


class ProviderDependencyHealth(BaseModel):
    """Prompt、工具、知识库或路由等一项依赖实况。"""

    dependency_id: str
    available: bool
    reason: str = ""


class ProviderHealthReport(BaseModel):
    """一个 Provider 的可解释健康结果。"""

    provider_id: str
    status: ProviderHealthStatus
    dependencies: list[ProviderDependencyHealth] = Field(default_factory=list)
    checked_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class CapabilityHealthMonitor:
    """由依赖实况生成 Resolver 可直接消费的健康目录。"""

    def apply(
        self,
        providers: list[CapabilityProviderDescriptor],
        reports: list[ProviderHealthReport],
    ) -> list[CapabilityProviderDescriptor]:
        """只改写运行健康投影，不改动发布资格、权限或优先级。"""
        by_id = {item.provider_id: item for item in reports}
        projected = []
        for provider in providers:
            report = by_id.get(provider.provider_id)
            if report is None:
                projected.append(provider)
                continue
            projected.append(
                provider.model_copy(
                    update={
                        "healthy": report.status
                        not in {
                            ProviderHealthStatus.UNAVAILABLE,
                            ProviderHealthStatus.QUARANTINED,
                        },
                        "health_status": report.status,
                    }
                )
            )
        return projected
