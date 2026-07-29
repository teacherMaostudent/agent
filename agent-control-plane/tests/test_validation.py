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
