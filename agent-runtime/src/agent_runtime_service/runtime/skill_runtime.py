"""受控 Skill 执行边界。

SkillRuntime 不拥有开放式任务规划，也不创建 Gateway 客户端。它只解析发布期冻结的
Skill 工件、缩小调用权限与预算、校验输入输出，并将受限执行请求交给已有 Graph 或
Workflow 节点。因此 Skill 不会演变为绕过平台治理的“小 Agent”。
"""

from __future__ import annotations

import json
from typing import Any, Protocol
from uuid import uuid4

from jsonschema import Draft202012Validator
from platform_sdk.contracts.execution import ExecutionContext
from platform_sdk.contracts.orchestration import SkillExecutionStatus
from platform_sdk.contracts.rag import RagSearchRequest
from platform_sdk.contracts.skills import (
    CompiledSkillPlan,
    SkillActivationPolicy,
    SkillBinding,
    SkillCard,
    SkillGovernanceProfile,
    validate_skill_composition,
)
from platform_sdk.tools.registry import ToolContext
from pydantic import BaseModel, Field

from agent_runtime_service.runtime.models import RuntimeBudget, RuntimeLimitExceeded


class SkillCatalog(Protocol):
    """只读 Skill Registry 契约；生产实现须从 Control Plane 读取精确工件。"""

    def resolve(self, tenant_id: str, binding: SkillBinding) -> CompiledSkillPlan:
        """返回已发布的精确版本，不支持按名称或 latest 搜索。"""
        ...

    def cards(self, tenant_id: str, capability_id: str = "") -> list[SkillCard]:
        """只返回最小 Skill Card，选中后才允许加载完整计划。"""
        ...


class SkillExecutor(Protocol):
    """Graph/Workflow 对接的受限执行协议，不向 Skill 暴露基础设施客户端。"""

    def execute(self, plan: CompiledSkillPlan, request: SkillExecutionRequest) -> dict[str, Any]:
        """通过既有 LLM、RAG 与 Tool 节点执行一个有界 Skill 调用。"""
        ...


class SkillInvocationError(RuntimeError):
    """Skill 绑定、预算、权限或 Schema 不满足时的失败关闭错误。"""


class SkillExecutionRequest(BaseModel):
    """一次 Skill 调用的有界输入；不携带 Agent Session 或任意网络地址。"""

    invocation_id: str = Field(default_factory=lambda: f"skill_{uuid4().hex}")
    binding: SkillBinding
    capability_id: str = Field(min_length=2, max_length=160)
    input: dict[str, Any] = Field(default_factory=dict)
    context: ExecutionContext
    caller_permissions: frozenset[str] = frozenset()
    agent_permissions: frozenset[str] = frozenset()
    invocation_depth: int = Field(default=0, ge=0)
    active_skills: int = Field(default=0, ge=0)
    effective_max_cost_usd: float = Field(default=0.0, ge=0)
    plan_id: str = ""
    plan_admission_id: str = ""
    step_id: str = ""


class SkillExecutionResult(BaseModel):
    """校验后的 Skill 输出与可审计工件引用，不复制大块原始上下文。"""

    invocation_id: str
    skill_id: str
    version: str
    artifact_digest: str
    status: SkillExecutionStatus = SkillExecutionStatus.COMPLETED
    output: dict[str, Any]
    effective_permissions: list[str]
    artifact_refs: list[str] = Field(default_factory=list)


class GovernedSkillRuntime:
    """执行精确 SkillVersion 的最小内核，不参与 Harness 生命周期协调。"""

    def __init__(self, catalog: SkillCatalog, executor: SkillExecutor) -> None:
        """注入只读目录与既有执行节点；二者均不能由请求或模型动态替换。"""
        self._catalog = catalog
        self._executor = executor

    def execute(
        self, request: SkillExecutionRequest, budget: RuntimeBudget
    ) -> SkillExecutionResult:
        """校验冻结工件及局部配额后执行，并对输出再次按发布 Schema 失败关闭。"""
        plan = self._catalog.resolve(request.context.tenant_id, request.binding)
        self._validate_plan(plan, request)
        self._validate_governance_preconditions(plan, request)
        max_cost = self._validate_limits(plan, request, budget)
        effective_permissions = self._effective_permissions(plan, request)
        effective_request = request.model_copy(
            update={
                "effective_max_cost_usd": max_cost,
                "context": SkillContextBuilder().build(request.context, request.invocation_id),
            }
        )
        output = self._executor.execute(plan, effective_request)
        if not isinstance(output, dict):
            raise SkillInvocationError("skill executor must return an object")
        self._validate_schema(plan.output_schema, output, "output")
        self._validate_governance_postconditions(plan, output)
        artifacts = output.pop("artifact_refs", [])
        if not isinstance(artifacts, list) or not all(isinstance(item, str) for item in artifacts):
            raise SkillInvocationError("artifact_refs must be a list of references")
        return SkillExecutionResult(
            invocation_id=request.invocation_id,
            skill_id=plan.skill_id,
            version=plan.version,
            artifact_digest=plan.artifact_digest,
            output=output,
            effective_permissions=sorted(effective_permissions),
            artifact_refs=artifacts,
        )

    @staticmethod
    def _validate_plan(plan: CompiledSkillPlan, request: SkillExecutionRequest) -> None:
        """比对 ID、版本、摘要、能力和激活策略，阻止 Registry 漂移或越权自动调用。"""
        binding = request.binding
        if (plan.skill_id, plan.version, plan.artifact_digest) != (
            binding.skill_id,
            binding.version,
            binding.artifact_digest,
        ):
            raise SkillInvocationError(
                "resolved skill artifact does not match the published binding"
            )
        capabilities = {item.capability_id for item in plan.provides}
        if request.capability_id.strip().upper() not in capabilities:
            raise SkillInvocationError("skill does not provide the requested capability")
        if plan.activation == SkillActivationPolicy.WORKFLOW_ONLY and (
            request.context.orchestration_owner.value != "workflow"
            or request.context.workflow_id == "direct-skill-invocation"
        ):
            raise SkillInvocationError(
                "workflow-only skill cannot be invoked by an agent-owned task"
            )
        GovernedSkillRuntime._validate_schema(plan.input_schema, request.input, "input")

    @staticmethod
    def _validate_limits(
        plan: CompiledSkillPlan, request: SkillExecutionRequest, budget: RuntimeBudget
    ) -> float:
        """只允许 Skill 缩小父任务资源边界，绝不自行为自身增加预算。"""
        if request.invocation_depth > plan.composition.max_skill_depth:
            raise SkillInvocationError(
                "skill invocation depth exceeds the published composition policy"
            )
        if request.active_skills >= plan.composition.max_active_skills:
            raise SkillInvocationError(
                "active skill count exceeds the published composition policy"
            )
        max_cost = min(budget.remaining_cost_usd, budget.max_cost_usd) * min(
            request.binding.max_budget_fraction, plan.composition.max_budget_fraction
        )
        if max_cost <= 0:
            raise RuntimeLimitExceeded(
                "SKILL_BUDGET_EXCEEDED", "skill budget fraction is exhausted"
            )
        return max_cost

    @staticmethod
    def _effective_permissions(plan: CompiledSkillPlan, request: SkillExecutionRequest) -> set[str]:
        """取调用者、Agent 与 Skill 所需权限的交集；空集合不能被解释为放行。"""
        requested = {permission for tool in plan.tools for permission in tool.required_permissions}
        parent = set(request.caller_permissions) & set(request.agent_permissions)
        if requested and not requested <= parent:
            raise SkillInvocationError(
                "caller and agent permissions do not cover the skill tool contract"
            )
        return parent & requested if requested else parent

    @staticmethod
    def _validate_schema(schema: dict[str, Any], value: dict[str, Any], label: str) -> None:
        """在外部调用前后验证 JSON Schema，拒绝格式漂移而不是让下游猜测。"""
        if not schema:
            return
        errors = list(Draft202012Validator(schema).iter_errors(value))
        if errors:
            raise SkillInvocationError(f"skill {label} violates schema: {errors[0].message}")

    @staticmethod
    def _validate_governance_preconditions(
        plan: CompiledSkillPlan, request: SkillExecutionRequest
    ) -> None:
        """执行受支持的确定性前置规则，未知规则不得被接受后静默忽略。"""
        supported = {"input.non_empty"}
        unknown = sorted(set(plan.governance_controls.precondition_ids) - supported)
        if unknown:
            raise SkillInvocationError(f"unsupported skill precondition: {unknown[0]}")
        if "input.non_empty" in plan.governance_controls.precondition_ids and not request.input:
            raise SkillInvocationError("skill precondition failed: input.non_empty")

    @staticmethod
    def _validate_governance_postconditions(
        plan: CompiledSkillPlan, output: dict[str, Any]
    ) -> None:
        """验证输出与显式 Verifier；高风险 Skill 不允许只依赖模型自评。"""
        supported_postconditions = {"output.non_empty"}
        unknown = sorted(set(plan.governance_controls.postcondition_ids) - supported_postconditions)
        if unknown:
            raise SkillInvocationError(f"unsupported skill postcondition: {unknown[0]}")
        if "output.non_empty" in plan.governance_controls.postcondition_ids and not output:
            raise SkillInvocationError("skill postcondition failed: output.non_empty")
        verifier_id = plan.governance_controls.verifier_id
        supported_verifiers = {"", "output-schema/v1", "non-empty/v1"}
        if verifier_id not in supported_verifiers:
            raise SkillInvocationError(f"unsupported skill verifier: {verifier_id}")
        if verifier_id == "non-empty/v1" and not output:
            raise SkillInvocationError("skill verifier failed: non-empty/v1")
        if (
            plan.governance_profile
            in {
                SkillGovernanceProfile.HIGH_RISK_WRITE,
                SkillGovernanceProfile.HUMAN_APPROVAL_REQUIRED,
            }
            and not verifier_id
        ):
            raise SkillInvocationError("high-risk skill requires a deterministic verifier")


class InMemorySkillCatalog:
    """测试与本地开发目录；生产路径必须由 Control Plane 的冻结版本工件替代。"""

    def __init__(self, plans: list[CompiledSkillPlan]) -> None:
        """按三元身份建立不可变索引，重复工件被视为配置错误。"""
        self._plans = {(item.skill_id, item.version, item.artifact_digest): item for item in plans}

    def resolve(self, tenant_id: str, binding: SkillBinding) -> CompiledSkillPlan:
        """忽略本地租户数据但保留协议参数，找不到精确工件即失败关闭。"""
        del tenant_id
        try:
            return self._plans[(binding.skill_id, binding.version, binding.artifact_digest)]
        except KeyError as exc:
            raise SkillInvocationError("published skill artifact is unavailable") from exc

    def cards(self, tenant_id: str, capability_id: str = "") -> list[SkillCard]:
        """实现两阶段披露：目录枚举不暴露 Prompt、工具绑定和知识过滤条件。"""
        del tenant_id
        capability = capability_id.strip().upper()
        cards = []
        for item in self._plans.values():
            provided = [value.capability_id for value in item.provides]
            if capability and capability not in provided:
                continue
            cards.append(
                SkillCard(
                    skill_id=item.skill_id,
                    version=item.version,
                    description=item.description,
                    provides=provided,
                    risk=item.risk,
                )
            )
        return sorted(cards, key=lambda item: (item.skill_id, item.version))


class SkillContextBuilder:
    """从 Workflow/Agent/API 的 ExecutionContext 构造一次性 Skill 上下文。"""

    def build(self, parent: ExecutionContext, invocation_id: str) -> ExecutionContext:
        """保留 RootTask/Owner/身份/截止时间，只添加 Skill Execution ID。"""
        return parent.model_copy(update={"skill_execution_id": invocation_id})


class SkillCompositionManager:
    """在执行前验证 Skill 依赖图、深度、数量和相邻 Schema 兼容性。"""

    def validate(self, root: CompiledSkillPlan, dependencies: list[CompiledSkillPlan]) -> None:
        """调用共享发布校验，并补充本次根 Skill 的局部深度/声明检查。"""
        if len(dependencies) > root.composition.max_skill_depth:
            raise SkillInvocationError("skill composition exceeds max_skill_depth")
        allowed = set(root.composition.allowed_dependencies)
        undeclared = [item.skill_id for item in dependencies if item.skill_id not in allowed]
        if undeclared:
            raise SkillInvocationError(f"skill dependency is not declared: {undeclared[0]}")
        try:
            validate_skill_composition([root, *dependencies])
        except ValueError as exc:
            raise SkillInvocationError(str(exc)) from exc


class GatewaySkillExecutor:
    """经既有 RuntimeContext 调用 RAG、Tool Gateway 和 LLM Gateway 的受控 Skill 执行器。"""

    def __init__(self, runtime_context) -> None:
        """只接收启动期冻结的强类型 Runtime 能力视图。"""
        self._context = runtime_context

    def execute(self, plan: CompiledSkillPlan, request: SkillExecutionRequest) -> dict[str, Any]:
        """按知识→显式工具输入→模型的固定管线执行，不让模型发明未绑定动作。"""
        evidence = []
        query = str(request.input.get("query") or request.input.get("task") or "").strip()
        for binding in plan.knowledge:
            if not query:
                raise SkillInvocationError("knowledge-bound skill requires task or query input")
            response = self._context.require_retrieval().search(
                RagSearchRequest(
                    query=query,
                    tenant_id=request.context.tenant_id,
                    user_id=request.context.user_id,
                    metadata={
                        "knowledge_base": binding.knowledge_base,
                        "filters": binding.filters,
                    },
                    index_version=binding.index_version,
                    embedding_contract_id=binding.embedding_contract_id,
                )
            )
            evidence.extend(item.model_dump(mode="json") for item in response.evidence)
        tool_results: dict[str, Any] = {}
        tool_inputs = request.input.get("tool_inputs", {})
        if not isinstance(tool_inputs, dict):
            raise SkillInvocationError("tool_inputs must be an object")
        for binding in plan.tools:
            if binding.tool_name not in tool_inputs:
                raise SkillInvocationError(f"bound tool input is missing: {binding.tool_name}")
            operation_id = f"{request.invocation_id}:{binding.tool_name}:{binding.version}"
            context = ToolContext(
                tenant_id=request.context.tenant_id,
                user_id=request.context.user_id,
                permissions=request.caller_permissions & request.agent_permissions,
                request_id=request.context.request_id,
                trace_id=request.context.trace_id,
                run_id=request.context.run_id,
                root_task_id=request.context.root_task_id,
                operation_id=operation_id,
                step_id=request.step_id or request.invocation_id,
                plan_id=request.plan_id,
                plan_admission_id=request.plan_admission_id,
                idempotency_key=operation_id,
                session_id=request.context.session_id,
                snapshot_id=request.context.snapshot_id,
                deadline_at=request.context.deadline_at.isoformat(),
                attempt_budget_remaining=request.context.attempt_budget_remaining,
                tool_version=binding.version,
            )
            tool_results[binding.tool_name] = self._context.tools.execute(
                binding.tool_name, tool_inputs[binding.tool_name], context
            )
        payload = {"input": request.input, "evidence": evidence, "tool_results": tool_results}
        headers = {
            **request.context.headers(),
            "X-Cost-Budget": str(request.effective_max_cost_usd),
        }
        return self._context.require_llm().complete_json(
            plan.logical_model,
            plan.instructions.system_template,
            json.dumps(payload, ensure_ascii=False),
            execution_headers=headers,
        )
