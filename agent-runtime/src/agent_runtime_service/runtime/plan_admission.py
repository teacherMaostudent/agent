"""计划级准入检查。

Planner 只能提出计划。本模块在 Execution Engine 启动前校验计划结构、能力、工具
范围、风险、预算和截止时间，并生成可审计准入凭证。该凭证不授权任何具体副作用；
动作级最终授权仍由 Tool Gateway 根据最新参数、身份、审批和策略完成。
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from platform_sdk.contracts.orchestration import (
    PlanAdmissionCheck,
    PlanAdmissionDecision,
)

from agent_runtime_service.runtime.models import (
    AdmittedExecutionPlan,
    ProposedExecutionPlan,
    RouteType,
)


class PlanAdmissionRejected(RuntimeError):
    """计划未通过确定性准入且不得进入执行引擎。"""

    def __init__(self, decision: PlanAdmissionDecision) -> None:
        """保留完整决定，供 API、Trace 和审计事件输出同一事实。"""
        self.decision = decision
        failed = ", ".join(item.check for item in decision.checks if not item.passed)
        super().__init__(f"execution plan admission rejected: {failed}")


class PlanAdmissionService:
    """使用已发布工件与当前不可变身份事实执行计划级 Reference Monitor。"""

    def admit(
        self,
        proposed: ProposedExecutionPlan,
        *,
        compiled_plan: dict[str, Any],
        caller_permissions: Iterable[str],
    ) -> AdmittedExecutionPlan:
        """返回准入计划；任一硬检查失败时携带决定失败关闭。"""
        permissions = {item.strip() for item in caller_permissions if item.strip()}
        tools = [item for item in compiled_plan.get("tools", []) if isinstance(item, dict)]
        providers = [
            item for item in compiled_plan.get("capability_providers", []) if isinstance(item, dict)
        ]
        published_contract = bool(compiled_plan.get("contract_hash"))
        tool_scope = self._allowed_tool_scope(tools, permissions)
        checks = [
            PlanAdmissionCheck(
                check="schema",
                passed=bool(proposed.plan_id and proposed.plan_hash),
                reason="计划必须具有稳定标识与内容摘要",
            ),
            PlanAdmissionCheck(
                check="executor",
                passed=not published_contract
                or proposed.executor_profile == str(compiled_plan.get("executor_profile", "")),
                reason="提议计划只能使用发布快照冻结的执行 Profile",
                facts={"published_contract": published_contract},
            ),
            self._capability_check(proposed, providers, enforce=published_contract),
            self._tool_scope_check(proposed, tools, tool_scope, enforce=published_contract),
            self._risk_check(tools, enforce=published_contract),
            PlanAdmissionCheck(
                check="budget",
                passed=proposed.cost.feasible
                and proposed.cost.remaining_cost_usd >= 0
                and proposed.budget_policy.max_cost_usd >= proposed.cost.estimated_cost_usd,
                reason="估算成本必须位于父任务剩余额度和计划上限内",
                facts={
                    "estimated_cost_usd": proposed.cost.estimated_cost_usd,
                    "remaining_cost_usd": proposed.cost.remaining_cost_usd,
                },
            ),
            PlanAdmissionCheck(
                check="deadline",
                passed=proposed.sla.feasible and proposed.sla.remaining_ms > 0,
                reason="计划开始前必须仍满足绝对截止时间",
                facts={"remaining_ms": proposed.sla.remaining_ms},
            ),
        ]
        decision = PlanAdmissionDecision(
            plan_id=proposed.plan_id,
            decision="ADMIT" if all(item.passed for item in checks) else "REJECT",
            policy_version=proposed.governance_policy.policy_version,
            checks=checks,
            allowed_tool_scope=tool_scope,
        )
        if not decision.admitted:
            raise PlanAdmissionRejected(decision)
        return AdmittedExecutionPlan(
            **proposed.model_dump(mode="python"),
            admission_id=decision.admission_id,
            admission_policy_version=decision.policy_version,
            admission_checks=decision.checks,
            allowed_tool_scope=decision.allowed_tool_scope,
        )

    @staticmethod
    def _allowed_tool_scope(tools: list[dict[str, Any]], permissions: set[str]) -> list[str]:
        """只把当前调用者拥有全部权限的冻结工具纳入计划级候选范围。"""
        allowed: list[str] = []
        for item in tools:
            required = {str(value).strip() for value in item.get("required_permissions", [])}
            name = str(item.get("tool_name") or item.get("name") or "").strip()
            if name and required.issubset(permissions):
                allowed.append(name)
        return sorted(set(allowed))

    @staticmethod
    def _capability_check(
        proposed: ProposedExecutionPlan,
        providers: list[dict[str, Any]],
        *,
        enforce: bool,
    ) -> PlanAdmissionCheck:
        """确认每项必需业务能力至少存在一个已冻结且合格的 Provider。"""
        available = {
            str(capability.get("capability_id", "")).strip().upper()
            for provider in providers
            if provider.get("qualified", True) and provider.get("healthy", True)
            for capability in provider.get("capabilities", [])
            if isinstance(capability, dict)
        }
        missing = sorted(set(proposed.capability_policy.required) - available)
        return PlanAdmissionCheck(
            check="capability_feasibility",
            passed=not enforce or not missing,
            reason="必需能力必须存在已发布且可用的 Provider",
            facts={"missing": missing},
        )

    @staticmethod
    def _tool_scope_check(
        proposed: ProposedExecutionPlan,
        tools: list[dict[str, Any]],
        allowed: list[str],
        *,
        enforce: bool,
    ) -> PlanAdmissionCheck:
        """工具路由至少需要一个当前身份可访问的冻结版本。"""
        needs_tool = proposed.route.route == RouteType.TOOL
        return PlanAdmissionCheck(
            check="tool_scope",
            passed=not enforce or not needs_tool or bool(allowed),
            reason="工具型计划只能进入调用者权限内的发布工具范围",
            facts={"published_count": len(tools), "allowed": allowed},
        )

    @staticmethod
    def _risk_check(tools: list[dict[str, Any]], *, enforce: bool) -> PlanAdmissionCheck:
        """拒绝发布契约中无法安全提交或重放的高风险工具。"""
        unsafe: list[str] = []
        for item in tools:
            name = str(item.get("tool_name") or item.get("name") or "<unknown>")
            risk = str(item.get("risk", "")).lower()
            side_effect = bool(item.get("side_effect")) or risk in {
                "high",
                "critical",
                "write_high_risk",
                "human_approval_required",
            }
            high_risk = risk in {
                "high",
                "critical",
                "write_high_risk",
                "human_approval_required",
            }
            if side_effect and not bool(item.get("idempotent", False)):
                unsafe.append(f"{name}:non_idempotent")
            if high_risk and not bool(item.get("approval_required", False)):
                unsafe.append(f"{name}:approval_missing")
        return PlanAdmissionCheck(
            check="risk",
            passed=not enforce or not unsafe,
            reason="副作用必须可幂等提交; 高风险动作必须声明审批",
            facts={"unsafe": sorted(unsafe)},
        )
