from __future__ import annotations

import re
from collections import deque

from app.domain.models import (
    AgentDraftSpec,
    IssueSeverity,
    TenantPolicy,
    ToolRisk,
    ValidationIssue,
    ValidationReport,
)

_PROMPT_VARIABLE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_.]*)\s*\}\}")


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


def _error(code: str, path: str, message: str) -> ValidationIssue:
    """构造会阻断发布的结构化校验问题，路径可直接定位到草稿字段。"""
    return ValidationIssue(severity=IssueSeverity.ERROR, code=code, path=path, message=message)


def _warning(code: str, path: str, message: str) -> ValidationIssue:
    """构造不阻断发布但需人工确认的结构化校验问题。"""
    return ValidationIssue(severity=IssueSeverity.WARNING, code=code, path=path, message=message)
