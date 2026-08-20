"""零 Agent Workflow 的固定步骤执行器。

此执行器只按照已编译步骤顺序推进。每一步通过 Capability Dispatcher 调用受控 Provider，
不会创建 Agent Session、调用 Planner 或让模型选择下一步。
"""

from __future__ import annotations

from typing import Any, Protocol

from jsonschema import Draft202012Validator
from platform_sdk.contracts.execution import ExecutionContext
from platform_sdk.contracts.orchestration import WorkflowExecutionStatus
from platform_sdk.contracts.skills import OrchestrationOwner
from platform_sdk.contracts.workflow import CompiledWorkflowPlan
from pydantic import BaseModel, Field


class WorkflowExecutionError(RuntimeError):
    """固定步骤无法安全推进、Schema 不匹配或 Owner 不一致时抛出。"""


class WorkflowSuspended(RuntimeError):
    """Human Provider 或外部信号需要持久等待时携带受控载荷。"""

    def __init__(self, reason: str, payload: dict[str, Any] | None = None) -> None:
        """保存结构化等待原因，调用方必须持久化后再返回客户端。"""
        super().__init__(reason)
        self.payload = payload or {}


class WorkflowCapabilityDispatcher(Protocol):
    """对接统一 Capability Resolver 的执行协议；具体 Tool/Skill/RAG 不属于 Workflow 内核。"""

    def dispatch(
        self, capability_id: str, payload: dict[str, Any], context: ExecutionContext
    ) -> dict[str, Any]:
        """执行一个已经由 Workflow 固定的能力步骤并返回结构化结果。"""
        ...


class WorkflowExecutionResult(BaseModel):
    """零 Agent Workflow 的顺序结果；每步输出以引用或结构化对象保存。"""

    workflow_id: str
    version: str
    status: WorkflowExecutionStatus = WorkflowExecutionStatus.COMPLETED
    step_outputs: dict[str, dict[str, Any]] = Field(default_factory=dict)
    next_step_index: int = Field(default=0, ge=0)
    pending_signal: dict[str, Any] = Field(default_factory=dict)
    compensated_steps: list[str] = Field(default_factory=list)


class ZeroAgentWorkflowRuntime:
    """执行已发布的固定工作流，不拥有重试、补偿或 Temporal 调度权。"""

    def __init__(self, dispatcher: WorkflowCapabilityDispatcher) -> None:
        """注入统一分派器，使 Workflow 不直接持有 Gateway 或 Provider 客户端。"""
        self._dispatcher = dispatcher

    def _dispatch(
        self, capability_id: str, payload: dict[str, Any], context: ExecutionContext
    ) -> dict[str, Any]:
        """同时接受协议对象和纯函数适配器，不改变 Workflow 的固定控制流。"""
        if callable(self._dispatcher):
            return self._dispatcher(capability_id, payload, context)
        return self._dispatcher.dispatch(capability_id, payload, context)

    def run(
        self,
        plan: CompiledWorkflowPlan,
        initial_input: dict[str, Any],
        context: ExecutionContext,
        *,
        checkpoint: WorkflowExecutionResult | None = None,
        signal: dict[str, Any] | None = None,
        embedded: bool = False,
        max_steps: int | None = None,
    ) -> WorkflowExecutionResult:
        """按冻结顺序推进，并可在每个 Temporal Activity 后返回检查点。

        ``max_steps`` 仅限制本次调用推进的步数，不修改发布计划。持久
        Workflow 传入 ``1``，使 Worker 崩溃后从 Temporal History 中的下一步
        恢复，而不是重放整条链。
        """
        if context.orchestration_owner != OrchestrationOwner.WORKFLOW and not embedded:
            raise WorkflowExecutionError(
                "zero-agent workflow requires orchestration_owner=workflow"
            )
        if context.workflow_id and context.workflow_id != plan.workflow_id:
            raise WorkflowExecutionError(
                "execution context workflow_id does not match the compiled plan"
            )
        outputs = dict(checkpoint.step_outputs) if checkpoint else {}
        start_index = checkpoint.next_step_index if checkpoint else 0
        if max_steps is not None and max_steps < 1:
            raise WorkflowExecutionError("max_steps must be positive when provided")
        end_index = (
            min(len(plan.steps), start_index + max_steps)
            if max_steps is not None
            else len(plan.steps)
        )
        for index, step in enumerate(plan.steps[start_index:end_index], start=start_index):
            payload = {
                "input": initial_input,
                "previous_steps": outputs,
                "signal": signal or {},
            }
            self._validate(step.input_schema, payload, step.step_id, "input")
            try:
                result = self._dispatch_with_retry(step, payload, context)
            except WorkflowSuspended as exc:
                return WorkflowExecutionResult(
                    workflow_id=plan.workflow_id,
                    version=plan.version,
                    status=WorkflowExecutionStatus.WAITING_SIGNAL,
                    step_outputs=outputs,
                    next_step_index=index,
                    pending_signal={"reason": str(exc), **exc.payload},
                )
            except Exception as exc:
                compensated = self._compensate(
                    plan, initial_input, outputs, context, completed_before=index
                )
                raise WorkflowExecutionError(
                    f"workflow step '{step.step_id}' failed after retries; "
                    f"compensated={compensated}: {exc}"
                ) from exc
            if not isinstance(result, dict):
                raise WorkflowExecutionError(
                    f"workflow step '{step.step_id}' returned a non-object result"
                )
            self._validate(step.output_schema, result, step.step_id, "output")
            outputs[step.step_id] = result
        if end_index < len(plan.steps):
            return WorkflowExecutionResult(
                workflow_id=plan.workflow_id,
                version=plan.version,
                status=WorkflowExecutionStatus.RUNNING,
                step_outputs=outputs,
                next_step_index=end_index,
                compensated_steps=(checkpoint.compensated_steps if checkpoint else []),
            )
        return WorkflowExecutionResult(
            workflow_id=plan.workflow_id,
            version=plan.version,
            step_outputs=outputs,
            next_step_index=len(plan.steps),
            compensated_steps=(checkpoint.compensated_steps if checkpoint else []),
        )

    def _dispatch_with_retry(
        self, step, payload: dict[str, Any], context: ExecutionContext
    ) -> dict[str, Any]:
        """按发布步骤的最大尝试数重试；幂等仍由 Provider/Gateway 保证。"""
        last_error: Exception | None = None
        for _ in range(step.max_attempts):
            try:
                return self._dispatch(step.capability_id, payload, context)
            except WorkflowSuspended:
                raise
            except Exception as exc:
                last_error = exc
        assert last_error is not None
        raise last_error

    def _compensate(
        self,
        plan: CompiledWorkflowPlan,
        initial_input: dict[str, Any],
        outputs: dict[str, dict[str, Any]],
        context: ExecutionContext,
        *,
        completed_before: int,
    ) -> list[str]:
        """逆序执行已完成步骤的显式补偿能力，未声明时不猜测回滚。"""
        compensated: list[str] = []
        for step in reversed(plan.steps[:completed_before]):
            if not step.compensation_capability_id:
                continue
            self._dispatch(
                step.compensation_capability_id,
                {
                    "input": initial_input,
                    "step_output": outputs.get(step.step_id, {}),
                    "compensating_step_id": step.step_id,
                },
                context,
            )
            compensated.append(step.step_id)
        return compensated

    @staticmethod
    def _validate(
        schema: dict[str, Any], value: dict[str, Any], step_id: str, direction: str
    ) -> None:
        """在每个能力边界进行 Schema 校验，禁止下游格式漂移污染后续固定步骤。"""
        if not schema:
            return
        errors = list(Draft202012Validator(schema).iter_errors(value))
        if errors:
            raise WorkflowExecutionError(
                f"workflow step '{step_id}' {direction} violates schema: {errors[0].message}"
            )
