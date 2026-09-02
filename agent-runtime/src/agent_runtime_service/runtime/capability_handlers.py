"""将统一 Capability Provider 映射到现有受治理执行边界。"""

from collections.abc import Callable
from typing import Any

from platform_sdk.contracts.context import ContextAssembleRequest
from platform_sdk.contracts.execution import ExecutionContext
from platform_sdk.contracts.rag import RagSearchRequest
from platform_sdk.contracts.skills import (
    CapabilityProviderDescriptor,
    CapabilityProviderKind,
    CompiledSkillPlan,
    SkillBinding,
)
from platform_sdk.tools.registry import ToolContext

from agent_runtime_service.runtime.models import RuntimeBudget
from agent_runtime_service.runtime.skill_runtime import (
    GovernedSkillRuntime,
    InMemorySkillCatalog,
    SkillExecutionRequest,
)
from agent_runtime_service.runtime.workflow_runtime import WorkflowSuspended


class RuntimeCapabilityHandlers:
    """为一次 RootTask 创建 Tool/Skill/RAG/Agent/Human/Workflow 处理器集。"""

    def __init__(
        self,
        container: Any,
        *,
        permissions: frozenset[str],
        budget: RuntimeBudget,
        agent_runner: Callable[
            [CapabilityProviderDescriptor, dict[str, Any], ExecutionContext], dict[str, Any]
        ]
        | None = None,
        workflow_runner: Callable[
            [CapabilityProviderDescriptor, dict[str, Any], ExecutionContext], dict[str, Any]
        ]
        | None = None,
        plan_id: str = "",
        plan_admission_id: str = "",
        step_id: str = "",
    ) -> None:
        """绑定父权限与父预算；所有 Provider 只能继承或缩小这两者。"""
        self._container = container
        self._permissions = permissions
        self._budget = budget
        self._agent_runner = agent_runner
        self._workflow_runner = workflow_runner
        self._plan_id = plan_id
        self._plan_admission_id = plan_admission_id
        self._step_id = step_id

    def handlers(self) -> dict[CapabilityProviderKind, Callable[..., dict[str, Any]]]:
        """仅暴露已部署的类型，未提供子运行器时 Resolver 结果会失败关闭。"""
        result: dict[CapabilityProviderKind, Callable[..., dict[str, Any]]] = {
            CapabilityProviderKind.TOOL: self._tool,
            CapabilityProviderKind.SKILL: self._skill,
            CapabilityProviderKind.RAG: self._rag,
            CapabilityProviderKind.MEMORY: self._memory,
            CapabilityProviderKind.HUMAN: self._human,
        }
        if self._agent_runner is not None:
            result[CapabilityProviderKind.AGENT] = self._agent_runner
        if self._workflow_runner is not None:
            result[CapabilityProviderKind.WORKFLOW] = self._workflow_runner
        return result

    def _tool(
        self,
        provider: CapabilityProviderDescriptor,
        payload: dict[str, Any],
        context: ExecutionContext,
    ) -> dict[str, Any]:
        """经 Tool Gateway 执行精确版本，保留 AuthZ、审批、幂等和副作用屏障。"""
        operation_id = f"{context.run_id}:{self._step_id}:{provider.provider_id}:{provider.version}"
        tool_context = ToolContext(
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            permissions=self._permissions,
            request_id=context.request_id,
            trace_id=context.trace_id,
            run_id=context.run_id,
            root_task_id=context.root_task_id,
            operation_id=operation_id,
            step_id=self._step_id,
            plan_id=self._plan_id,
            plan_admission_id=self._plan_admission_id,
            idempotency_key=operation_id,
            session_id=context.session_id,
            snapshot_id=context.snapshot_id,
            release_id=context.release_id,
            release_stage=context.release_stage,
            release_projection_revision=context.release_projection_revision,
            traffic_policy_version=context.traffic_policy_version,
            side_effect_policy_version=context.side_effect_policy_version,
            deadline_at=context.deadline_at.isoformat(),
            attempt_budget_remaining=context.attempt_budget_remaining,
            tool_version=provider.version,
        )
        result = self._container.runtime_context.tools.execute(
            provider.provider_id, payload, tool_context
        )
        return result if isinstance(result, dict) else {"result": result}

    def _skill(
        self,
        provider: CapabilityProviderDescriptor,
        payload: dict[str, Any],
        context: ExecutionContext,
    ) -> dict[str, Any]:
        """从 Control Plane 取得精确 Active Skill 工件，再交由 SkillRuntime 执行。"""
        resolution = self._container.control_plane.resolve_skill(
            context.tenant_id, provider.provider_id, provider.version, context.trace_id
        )
        plan = CompiledSkillPlan.model_validate(resolution.get("plan"))
        digest = str(resolution.get("artifact_digest", ""))
        if provider.artifact_digest and digest != provider.artifact_digest:
            raise RuntimeError("Skill provider artifact digest drift")
        binding = SkillBinding(
            skill_id=provider.provider_id,
            version=provider.version,
            artifact_digest=digest,
            max_budget_fraction=1.0,
        )
        provided_capability_ids = {value.capability_id for value in plan.provides}
        matched_capability_id = next(
            (
                item.capability_id
                for item in provider.capabilities
                if item.capability_id in provided_capability_ids
            ),
            "",
        )
        if not matched_capability_id:
            # Provider 描述与冻结 Skill 工件不一致时必须明确失败，
            # 不能让 StopIteration 泄漏为不可解释的 500。
            raise RuntimeError("Skill provider capability is absent from the frozen artifact")
        result = GovernedSkillRuntime(
            InMemorySkillCatalog([plan]), self._container.skill_executor
        ).execute(
            SkillExecutionRequest(
                binding=binding,
                capability_id=matched_capability_id,
                input=payload,
                context=context.model_copy(
                    update={"skill_execution_id": f"skill-{context.run_id}-{provider.provider_id}"}
                ),
                caller_permissions=self._permissions,
                agent_permissions=self._permissions,
                plan_id=self._plan_id,
                plan_admission_id=self._plan_admission_id,
                step_id=self._step_id,
            ),
            self._budget,
        )
        return result.output

    def _rag(
        self,
        provider: CapabilityProviderDescriptor,
        payload: dict[str, Any],
        context: ExecutionContext,
    ) -> dict[str, Any]:
        """经 RAG 服务执行 ACL 检索，并强制冻结索引/向量契约。"""
        query = str(payload.get("query") or payload.get("task") or "").strip()
        if not query:
            raise ValueError("RAG capability requires query or task")
        response = self._container.runtime_context.require_retrieval().search(
            RagSearchRequest(
                query=query,
                tenant_id=context.tenant_id,
                user_id=context.user_id,
                metadata={
                    "knowledge_base": provider.provider_id,
                    "knowledge_version": provider.version,
                },
                index_version=provider.rag_index_version,
                embedding_contract_id=provider.embedding_contract_id,
            )
        )
        return {
            "evidence": [item.model_dump(mode="json") for item in response.evidence],
            "index_version": response.index_version,
            "embedding_contract_id": response.embedding_contract_id,
        }

    def _memory(
        self,
        provider: CapabilityProviderDescriptor,
        payload: dict[str, Any],
        context: ExecutionContext,
    ) -> dict[str, Any]:
        """经 Context Service 读取授权会话记忆，不直连会话数据库。"""
        query = str(payload.get("query") or payload.get("task") or provider.provider_id)
        package = self._container.runtime_context.context.assemble(
            ContextAssembleRequest(
                session_id=context.session_id,
                query=query,
                tenant_id=context.tenant_id,
                user_id=context.user_id,
                include_rag=False,
                rag_required=False,
            ),
            execution_headers=context.headers(),
        )
        return {
            "messages": [item.model_dump(mode="json") for item in package.recent_messages],
            "user_context": package.user_context,
            "truncated": package.truncated,
        }

    @staticmethod
    def _human(
        provider: CapabilityProviderDescriptor,
        payload: dict[str, Any],
        context: ExecutionContext,
    ) -> dict[str, Any]:
        """人工 Provider 不伪造同步答案，必须挂起 Workflow 等待受信号恢复。"""
        del context
        if payload.get("signal"):
            return {"human_decision": payload["signal"]}
        raise WorkflowSuspended(
            "human_provider_signal_required",
            {"provider_id": provider.provider_id},
        )
