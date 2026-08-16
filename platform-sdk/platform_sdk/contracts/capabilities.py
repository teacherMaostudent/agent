"""发布快照所需 Runtime 能力的共享、确定性声明。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator


class RuntimeCapability(StrEnum):
    """Runtime 可提供给已发布执行计划的固定外围能力。"""

    CONTEXT = "context"
    LLM = "llm"
    RETRIEVAL = "retrieval"
    TOOL = "tool"
    WORKFLOW = "workflow"
    SESSION = "session"
    SUBAGENT = "subagent"
    SANDBOX = "sandbox"
    CODE_RUNNER = "code_runner"


CAPABILITY_CONTRACT_VERSION = "runtime-capability-contract/v1"


class CapabilityManifest(BaseModel):
    """一个已部署 Runtime 能力的版本化、可校验声明。

    Manifest 描述的是 Provider 可提供的受控接口，而不是让 Agent 在请求中注册插件。
    Control Plane 固定其摘要，Runtime 在运行前据此证明能力、隔离等级和执行器兼容性。
    """

    capability: RuntimeCapability
    provider_id: str = Field(min_length=3, max_length=160)
    contract_version: str = Field(default=CAPABILITY_CONTRACT_VERSION, min_length=1, max_length=80)
    provider_version: str = Field(default="v1", min_length=1, max_length=80)
    artifact_digest: str = Field(min_length=16, max_length=128)
    executor_profiles: tuple[str, ...] = ()
    data_regions: tuple[str, ...] = ()
    isolation: str = Field(default="service", pattern=r"^(service|process|sandbox)$")
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)

    @field_validator("executor_profiles", "data_regions")
    @classmethod
    def normalize_unique_values(cls, value: Sequence[str]) -> tuple[str, ...]:
        """统一目录值的顺序并拒绝空项，使 Manifest 摘要跨实例稳定。"""
        normalized = tuple(sorted({str(item).strip() for item in value if str(item).strip()}))
        return normalized

    def canonical_payload(self) -> dict[str, Any]:
        """生成不依赖 Pydantic 表示的稳定 Payload，供发布和部署证明计算摘要。"""
        return self.model_dump(mode="json")


def capability_manifest_digest(manifests: Sequence[CapabilityManifest]) -> str:
    """计算能力清单的顺序无关摘要，防止目录版本相同但 Provider 已漂移。"""
    payload = [item.canonical_payload() for item in sorted(manifests, key=lambda item: item.capability.value)]
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def required_runtime_capabilities(spec: Mapping[str, Any]) -> list[str]:
    """从 Agent Spec 推导运行所需能力，避免快照手工填写后发生漂移。

    推导规则故意保守且可审计：普通声明式图需要 Context 与 LLM，知识和工具绑定
    分别要求检索和工具能力，Temporal Profile 还必须有耐久 Workflow 调度能力。
    ``simple/v1`` 是唯一明确的无外围依赖短任务执行器。
    """
    profile = str(spec.get("runtime_executor") or "declarative-langgraph/v1").strip()
    if profile == "simple/v1":
        return []

    required = {RuntimeCapability.CONTEXT.value, RuntimeCapability.LLM.value}
    if spec.get("knowledge"):
        required.add(RuntimeCapability.RETRIEVAL.value)
    if spec.get("tools"):
        required.add(RuntimeCapability.TOOL.value)
    if spec.get("subagents"):
        required.add(RuntimeCapability.SUBAGENT.value)
    if profile == "temporal-workflow/v1":
        required.add(RuntimeCapability.WORKFLOW.value)
    if profile == "code-runner/v1":
        required.update({RuntimeCapability.CODE_RUNNER.value, RuntimeCapability.SANDBOX.value})
    return sorted(required)
