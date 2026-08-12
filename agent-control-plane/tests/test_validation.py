from __future__ import annotations

from copy import deepcopy

from app.domain.models import AgentDraftSpec, TenantPolicy
from app.domain.validation import validate_agent_spec


def test_graph_validation_reports_unreachable_and_invalid_edge(
    valid_spec: dict[str, object],
) -> None:
    invalid = deepcopy(valid_spec)
    invalid["graph"]["nodes"].append({"node_id": "orphan", "kind": "llm"})
    invalid["graph"]["edges"].append({"from_node": "missing", "to_node": "respond"})

    report = validate_agent_spec(
        AgentDraftSpec.model_validate(invalid),
        TenantPolicy(tenant_id="tenant-a"),
    )

    assert report.valid is False
    assert {issue.code for issue in report.issues} >= {
        "graph.edge_invalid",
        "graph.unreachable_nodes",
    }


def test_graph_validation_rejects_nodes_and_transitions_runtime_cannot_execute(
    valid_spec: dict[str, object],
) -> None:
    """发布前拒绝任意代码节点与检索直连工具等越过固定安全图的迁移。"""
    invalid = deepcopy(valid_spec)
    invalid["graph"] = {
        "graph_id": "invalid-workflow",
        "entrypoint": "start",
        "terminal_nodes": ["answer"],
        "nodes": [
            {"node_id": "start", "kind": "python_callback"},
            {"node_id": "retrieve", "kind": "retrieval"},
            {"node_id": "tool", "kind": "tool"},
            {"node_id": "answer", "kind": "answer"},
        ],
        "edges": [
            {"from_node": "start", "to_node": "retrieve"},
            {"from_node": "retrieve", "to_node": "tool"},
        ],
    }

    report = validate_agent_spec(
        AgentDraftSpec.model_validate(invalid),
        TenantPolicy(tenant_id="tenant-a"),
    )

    assert not report.valid
    assert {issue.code for issue in report.issues} >= {
        "graph.unsupported_node_kind",
        "graph.invalid_entry_role",
        "graph.transition_not_supported",
    }


def test_graph_validation_requires_safe_conditions_for_branches(
    valid_spec: dict[str, object],
) -> None:
    """分支边必须使用白名单 DSL；无条件分支或代码表达式不能发布。"""
    invalid = deepcopy(valid_spec)
    invalid["graph"] = {
        "graph_id": "conditional-workflow",
        "entrypoint": "decide",
        "terminal_nodes": ["answer", "clarify"],
        "nodes": [
            {"node_id": "decide", "kind": "decision"},
            {"node_id": "answer", "kind": "answer"},
            {"node_id": "clarify", "kind": "clarify"},
        ],
        "edges": [
            {"from_node": "decide", "to_node": "answer", "condition": "decision.action == ANSWER"},
            {"from_node": "decide", "to_node": "clarify"},
        ],
    }

    report = validate_agent_spec(
        AgentDraftSpec.model_validate(invalid),
        TenantPolicy(tenant_id="tenant-a"),
    )

    assert not report.valid
    assert {issue.code for issue in report.issues} >= {
        "graph.condition_invalid",
        "graph.ambiguous_conditions",
    }
