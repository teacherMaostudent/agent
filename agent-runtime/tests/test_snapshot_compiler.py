import pytest

from agent_runtime_service.runtime.snapshot_compiler import (
    SnapshotCompileError,
    compile_snapshot,
    render_prompt,
    validate_final_output,
    validate_tool_manifests,
)


def snapshot() -> dict:
    return {
        "schema_version": "1.0",
        "tenant_id": "tenant-a",
        "agent_id": "review-agent",
        "spec": {
            "graph": {
                "graph_id": "review-v1",
                "entrypoint": "retrieve",
                "terminal_nodes": ["answer"],
                "nodes": [
                    {"node_id": "retrieve", "kind": "retrieval"},
                    {"node_id": "answer", "kind": "answer"},
                ],
                "edges": [{"from_node": "retrieve", "to_node": "answer"}],
            },
            "prompt": {
                "system_template": "Review {{task}} for {{metadata.department}}.",
                "variables": ["task", "metadata.department"],
                "output_schema": {
                    "type": "object",
                    "required": ["answer"],
                    "properties": {"answer": {"type": "string"}},
                },
            },
            "tools": [
                {
                    "tool_name": "lookup",
                    "version": "1.0.0",
                    "risk": "read_only",
                    "approval_required": False,
                    "timeout_seconds": 20,
                }
            ],
            "knowledge": [
                {
                    "knowledge_base": "enterprise-documents",
                    "version": "2026.08",
                    "filters": {"status": "published"},
                    "top_k": 12,
                }
            ],
            "model_policy": {
                "default_route": "primary",
                "routes": [
                    {
                        "route_name": "primary",
                        "models": ["model-a", "model-b"],
                        "data_region": "cn",
                    }
                ],
            },
        },
    }


def test_snapshot_compiles_every_runtime_binding() -> None:
    plan = compile_snapshot(
        snapshot(),
        tenant_id="tenant-a",
        agent_id="review-agent",
        fallback_model="fallback",
    )

    assert plan.graph_execution_order == ["retrieve", "answer"]
    assert plan.retrieval_top_k == 12
    assert plan.logical_model == "model-a"
    assert plan.fallback_models == ["model-b"]
    assert (
        render_prompt(
            plan.model_dump(),
            {"task": "SOP", "metadata": {"department": "QA"}},
        )
        == "Review SOP for QA."
    )
    validate_final_output(plan.model_dump(), '{"answer":"ok"}')


def test_controlled_scan_binding_is_preserved_in_compiled_snapshot() -> None:
    data = snapshot()
    data["spec"]["tools"].append(
        {
            "tool_name": "controlled_scan",
            "version": "1.0.0",
            "risk": "read_only",
            "approval_required": False,
            "timeout_seconds": 30,
        }
    )
    plan = compile_snapshot(
        data, tenant_id="tenant-a", agent_id="review-agent", fallback_model="fallback"
    )
    assert any(item["tool_name"] == "controlled_scan" for item in plan.tools)


def test_snapshot_identity_and_tool_policy_drift_fail_closed() -> None:
    with pytest.raises(SnapshotCompileError, match="identity"):
        compile_snapshot(
            snapshot(),
            tenant_id="other",
            agent_id="review-agent",
            fallback_model="fallback",
        )

    plan = compile_snapshot(
        snapshot(),
        tenant_id="tenant-a",
        agent_id="review-agent",
        fallback_model="fallback",
    )
    with pytest.raises(SnapshotCompileError, match="policy drift"):
        validate_tool_manifests(
            plan.model_dump(),
            [
                {
                    "name": "lookup",
                    "version": "1.0.0",
                    "risk": "write_high_risk",
                    "approval_required": True,
                    "timeout_seconds": 20,
                }
            ],
        )
