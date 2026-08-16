from __future__ import annotations

import re
from collections import deque

from platform_sdk.contracts.workflow import WorkflowConditionError, compile_workflow_condition

from app.domain.models import (
    AgentDraftSpec,
    IssueSeverity,
    TenantPolicy,
    ToolRisk,
    ValidationIssue,
    ValidationReport,
)

_PROMPT_VARIABLE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_.]*)\s*\}\}")
_WORKFLOW_KINDS = {
    "decision",
    "planner",
    "retrieval",
    "rag",
    "tool",
    "action",
    "answer",
    "final",
    "clarify",
    "clarification",
    "llm",
}


def validate_agent_spec(spec: AgentDraftSpec, policy: TenantPolicy) -> ValidationReport:
    """以确定性规则校验图、Prompt、工具、知识和模型策略，不修改草稿。"""
    issues: list[ValidationIssue] = []

    node_ids = [node.node_id for node in spec.graph.nodes]
    node_set = set(node_ids)
    if len(node_ids) != len(node_set):
        issues.append(
            _error("graph.duplicate_node", "graph.nodes", "Graph node_id must be unique.")
        )

    if spec.graph.entrypoint not in node_set:
        issues.append(
            _error(
                "graph.entrypoint_missing",
                "graph.entrypoint",
                "Entrypoint must reference an existing node.",
            )
        )

    unknown_terminals = sorted(set(spec.graph.terminal_nodes) - node_set)
    if unknown_terminals:
        issues.append(
            _error(
                "graph.terminal_missing",
                "graph.terminal_nodes",
                f"Terminal nodes do not exist: {', '.join(unknown_terminals)}.",
            )
        )

    adjacency: dict[str, set[str]] = {node_id: set() for node_id in node_set}
    outgoing_conditions: dict[str, list[bool]] = {node_id: [] for node_id in node_set}
    for index, edge in enumerate(spec.graph.edges):
        if edge.from_node not in node_set or edge.to_node not in node_set:
            issues.append(
                _error(
                    "graph.edge_invalid",
                    f"graph.edges[{index}]",
                    "Both edge endpoints must reference existing nodes.",
                )
            )
            continue
        adjacency[edge.from_node].add(edge.to_node)
        try:
            compile_workflow_condition(edge.condition)
            has_condition = bool(edge.condition and edge.condition.strip())
            outgoing_conditions[edge.from_node].append(has_condition)
        except WorkflowConditionError as exc:
            issues.append(
                _error(
                    "graph.condition_invalid",
                    f"graph.edges[{index}].condition",
                    str(exc),
                )
            )

    roles = {
        node.node_id: _workflow_role(node.kind, terminal=node.node_id in spec.graph.terminal_nodes)
        for node in spec.graph.nodes
    }
    for index, node in enumerate(spec.graph.nodes):
        if node.kind.strip().lower() not in _WORKFLOW_KINDS:
            issues.append(
                _error(
                    "graph.unsupported_node_kind",
                    f"graph.nodes[{index}].kind",
                    f"Node kind '{node.kind}' is not executable by the governed Runtime.",
                )
            )
    entry_role = roles.get(spec.graph.entrypoint)
    if entry_role not in {"decision", "retrieval", "clarify"}:
        issues.append(
            _error(
                "graph.invalid_entry_role",
                "graph.entrypoint",
                "Entrypoint must map to decision, retrieval, or clarify.",
            )
        )
    for terminal in spec.graph.terminal_nodes:
        if roles.get(terminal) not in {"answer", "clarify"}:
            issues.append(
                _error(
                    "graph.invalid_terminal_role",
                    "graph.terminal_nodes",
                    "Terminal nodes must map to answer or clarify.",
                )
            )
        if adjacency.get(terminal):
            issues.append(
                _error(
                    "graph.terminal_has_successor",
                    "graph.edges",
                    "Terminal nodes cannot have outgoing edges.",
                )
            )
    _validate_workflow_transitions(roles, adjacency, outgoing_conditions, issues)

    if spec.graph.entrypoint in node_set:
        reachable = _reachable(spec.graph.entrypoint, adjacency)
        unreachable = sorted(node_set - reachable)
        if unreachable:
            issues.append(
                _error(
                    "graph.unreachable_nodes",
                    "graph.nodes",
                    f"Nodes are unreachable from entrypoint: {', '.join(unreachable)}.",
                )
            )
        if not reachable.intersection(spec.graph.terminal_nodes):
            issues.append(
                _error(
                    "graph.no_terminal_path",
                    "graph.terminal_nodes",
                    "No terminal node is reachable from the entrypoint.",
                )
            )

    declared_variables = set(spec.prompt.variables)
    used_variables = set(_PROMPT_VARIABLE.findall(spec.prompt.system_template))
    undeclared = sorted(used_variables - declared_variables)
    unused = sorted(declared_variables - used_variables)
    if undeclared:
        issues.append(
            _error(
                "prompt.undeclared_variables",
                "prompt.system_template",
                f"Template variables are not declared: {', '.join(undeclared)}.",
            )
        )
    if unused:
        issues.append(
            _warning(
                "prompt.unused_variables",
                "prompt.variables",
                f"Declared variables are not used: {', '.join(unused)}.",
            )
        )

    tool_keys = [(tool.tool_name, tool.version) for tool in spec.tools]
    if len(tool_keys) != len(set(tool_keys)):
        issues.append(
            _error("tools.duplicate", "tools", "Tool name and version bindings must be unique.")
        )
    if policy.require_approval_for_high_risk_tools:
        for index, tool in enumerate(spec.tools):
            if (
                tool.risk in {ToolRisk.WRITE_HIGH_RISK, ToolRisk.HUMAN_APPROVAL_REQUIRED}
                and not tool.approval_required
            ):
                issues.append(
                    _error(
                        "tools.approval_required",
                        f"tools[{index}].approval_required",
                        f"High-risk tool '{tool.tool_name}' requires human approval.",
                    )
                )

    knowledge_keys = [(item.knowledge_base, item.version) for item in spec.knowledge]
    if len(knowledge_keys) != len(set(knowledge_keys)):
        issues.append(
            _error(
                "knowledge.duplicate",
                "knowledge",
                "Knowledge base and version bindings must be unique.",
            )
        )

    subagent_ids = [item.agent_id for item in spec.subagents]
    if len(subagent_ids) != len(set(subagent_ids)):
        issues.append(
            _error(
                "subagents.duplicate",
                "subagents",
                "A published Agent may bind each subagent only once.",
            )
        )

    route_names = [route.route_name for route in spec.model_policy.routes]
    route_set = set(route_names)
    if len(route_names) != len(route_set):
        issues.append(
            _error(
                "models.duplicate_route", "model_policy.routes", "Model route names must be unique."
            )
        )
    if spec.model_policy.default_route not in route_set:
        issues.append(
            _error(
                "models.default_route_missing",
                "model_policy.default_route",
                "Default route must reference an existing route.",
            )
        )
    for index, route in enumerate(spec.model_policy.routes):
        if route.fallback_route and route.fallback_route not in route_set:
            issues.append(
                _error(
                    "models.fallback_route_missing",
                    f"model_policy.routes[{index}].fallback_route",
                    f"Fallback route '{route.fallback_route}' does not exist.",
                )
            )
        if policy.allowed_models:
            forbidden_models = sorted(set(route.models) - set(policy.allowed_models))
            if forbidden_models:
                issues.append(
                    _error(
                        "policy.model_not_allowed",
                        f"model_policy.routes[{index}].models",
                        f"Tenant policy does not allow: {', '.join(forbidden_models)}.",
                    )
                )
        if (
            route.data_region
            and policy.allowed_data_regions
            and route.data_region not in policy.allowed_data_regions
        ):
            issues.append(
                _error(
                    "policy.data_region_not_allowed",
                    f"model_policy.routes[{index}].data_region",
                    f"Tenant policy does not allow data region '{route.data_region}'.",
                )
            )

    limits = spec.runtime_limits
    if limits.max_llm_calls > limits.max_steps:
        issues.append(
            _warning(
                "limits.llm_calls_exceed_steps",
                "runtime_limits.max_llm_calls",
                "max_llm_calls exceeds max_steps; verify this is intentional.",
            )
        )
    if limits.max_tool_calls > limits.max_steps:
        issues.append(
            _warning(
                "limits.tool_calls_exceed_steps",
                "runtime_limits.max_tool_calls",
                "max_tool_calls exceeds max_steps; verify this is intentional.",
            )
        )

    return ValidationReport(
        valid=not any(issue.severity == IssueSeverity.ERROR for issue in issues),
        issues=issues,
    )


def _reachable(entrypoint: str, adjacency: dict[str, set[str]]) -> set[str]:
    """广度优先计算入口可达节点，用于阻止发布包含永远不会执行的步骤。"""
    visited: set[str] = set()
    queue = deque([entrypoint])
    while queue:
        node = queue.popleft()
        if node in visited:
            continue
        visited.add(node)
        queue.extend(adjacency.get(node, set()) - visited)
    return visited


def _workflow_role(kind: str, *, terminal: bool) -> str:
    """将 Control Plane Graph 节点映射为 Runtime 受限工作流角色。"""
    normalized = kind.strip().lower()
    if normalized in {"decision", "planner"}:
        return "decision"
    if normalized in {"retrieval", "rag"}:
        return "retrieval"
    if normalized in {"tool", "action"}:
        return "tool"
    if normalized in {"answer", "final"}:
        return "answer"
    if normalized in {"clarify", "clarification"}:
        return "clarify"
    if normalized == "llm":
        return "answer" if terminal else "decision"
    return "unsupported"


def _validate_workflow_transitions(
    roles: dict[str, str],
    adjacency: dict[str, set[str]],
    outgoing_conditions: dict[str, list[bool]],
    issues: list[ValidationIssue],
) -> None:
    """在发布期验证 Graph DSL 不会要求 Runtime 走越权或未实现的迁移。"""
    allowed = {
        "decision": {"retrieval", "tool", "answer", "clarify"},
        "retrieval": {"decision", "answer", "clarify"},
        "tool": {"decision", "answer", "clarify"},
        "answer": set(),
        "clarify": set(),
    }
    for node_id, targets in adjacency.items():
        role = roles[node_id]
        if role not in allowed:
            continue
        target_roles = {roles[target] for target in targets}
        if not target_roles <= allowed[role]:
            issues.append(
                _error(
                    "graph.transition_not_supported",
                    "graph.edges",
                    f"Node '{node_id}' has a transition Runtime cannot execute safely.",
                )
            )
        if role in {"retrieval", "tool"} and len(targets) != 1:
            issues.append(
                _error(
                    "graph.side_effect_successor_ambiguous",
                    "graph.edges",
                    f"{role} node '{node_id}' must have exactly one successor.",
                )
            )
        if role == "decision" and not targets:
            issues.append(
                _error(
                    "graph.decision_without_action",
                    "graph.edges",
                    f"Decision node '{node_id}' must declare at least one action.",
                )
            )
        # Multiple choices need explicit conditions. A single unconditioned
        # edge is deterministic; otherwise Runtime would have to guess.
        if len(targets) > 1 and not all(outgoing_conditions[node_id]):
            issues.append(
                _error(
                    "graph.ambiguous_conditions",
                    "graph.edges",
                    f"Branching node '{node_id}' requires a condition on every outgoing edge.",
                )
            )


def _error(code: str, path: str, message: str) -> ValidationIssue:
    """构造会阻断发布的结构化校验问题，路径可直接定位到草稿字段。"""
    return ValidationIssue(severity=IssueSeverity.ERROR, code=code, path=path, message=message)


def _warning(code: str, path: str, message: str) -> ValidationIssue:
    """构造不阻断发布但需人工确认的结构化校验问题。"""
    return ValidationIssue(severity=IssueSeverity.WARNING, code=code, path=path, message=message)
