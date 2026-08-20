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
    assert plan.workflow_policy["node_roles"] == {"retrieve": "retrieval", "answer": "answer"}
    assert plan.executor_profile == "declarative-langgraph/v1"
    assert plan.required_capabilities == ["context", "llm", "retrieval", "tool"]
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


def test_snapshot_compiles_execution_lifecycle_and_reasoning_independently() -> None:
    """长期可靠调度与 Agentic 推理是两个维度，不再依赖一个含混 Profile 名称。"""
    data = snapshot()
    data["spec"]["execution"] = {
        "lifecycle": "durable_workflow",
        "reasoning": "agentic",
    }

    plan = compile_snapshot(
        data, tenant_id="tenant-a", agent_id="review-agent", fallback_model="fallback"
    )

    assert plan.executor_profile == "temporal-agentic/v1"
    assert plan.execution_requirements.lifecycle.value == "durable_workflow"
    assert plan.execution_requirements.reasoning.value == "agentic"


def test_snapshot_compiles_engine_and_durability_as_orthogonal_dimensions() -> None:
    """LangGraph 可使用独立检查点而不被误建模成另一种 Agent Executor。"""
    data = snapshot()
    data["spec"]["execution"] = {
        "engine": "langgraph",
        "durability": "checkpointed",
        "planning_strategy": "plan_execute",
        "default_step_strategy": "deterministic",
        "adaptive_step_strategy": "react",
        "context_strategy": "managed_ledger",
        "tool_presentation": "native",
    }

    plan = compile_snapshot(
        data, tenant_id="tenant-a", agent_id="review-agent", fallback_model="fallback"
    )

    assert plan.executor_profile == "checkpointed-langgraph/v1"
    assert plan.execution_requirements.normalized_engine().value == "langgraph"
    assert plan.execution_requirements.normalized_durability().value == "checkpointed"


def test_deep_agents_requires_an_explicit_engine_profile() -> None:
    """旧 agentic 推理仍由 LangGraph 执行; 只有显式新引擎才能编译 Deep Agents Profile。"""
    data = snapshot()
    data["spec"]["execution"] = {
        "engine": "deep_agents",
        "durability": "ephemeral",
    }

    plan = compile_snapshot(
        data, tenant_id="tenant-a", agent_id="review-agent", fallback_model="fallback"
    )

    assert plan.executor_profile == "deep-agents/v1"
    assert plan.execution_requirements.normalized_engine().value == "deep_agents"


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


def test_snapshot_freezes_exact_skill_version_and_digest() -> None:
    """Skill 只能以版本和工件摘要被绑定，不能由 Runtime 临时选择最新定义。"""
    data = snapshot()
    data["spec"]["skills"] = [
        {
            "skill_id": "document-review",
            "version": "1.2.0",
            "artifact_digest": "a" * 64,
            "required": True,
            "max_invocations": 2,
            "max_budget_fraction": 0.2,
        }
    ]

    plan = compile_snapshot(
        data, tenant_id="tenant-a", agent_id="review-agent", fallback_model="fallback"
    )

    assert plan.skills == data["spec"]["skills"]
    assert "skill" in plan.required_capabilities


def test_snapshot_rejects_non_pinned_or_duplicate_skill_binding() -> None:
    """阻止 Skill 名称漂移与重复绑定，失败发生在模型调用和副作用之前。"""
    data = snapshot()
    data["spec"]["skills"] = [
        {
            "skill_id": "document-review",
            "version": "1.2.0",
            "artifact_digest": "a" * 64,
        },
        {
            "skill_id": "document-review",
            "version": "1.2.0",
            "artifact_digest": "b" * 64,
        },
    ]

    with pytest.raises(SnapshotCompileError, match="unique"):
        compile_snapshot(
            data, tenant_id="tenant-a", agent_id="review-agent", fallback_model="fallback"
        )


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


def test_snapshot_rejects_graph_that_cannot_be_mapped_to_safe_runtime_nodes() -> None:
    data = snapshot()
    data["spec"]["graph"]["nodes"][0]["kind"] = "python_callback"
    with pytest.raises(SnapshotCompileError, match="unsupported"):
        compile_snapshot(
            data, tenant_id="tenant-a", agent_id="review-agent", fallback_model="fallback"
        )


def test_snapshot_compiles_auditable_branch_conditions() -> None:
    data = snapshot()
    data["spec"]["graph"] = {
        "graph_id": "branch-v1",
        "entrypoint": "decide",
        "terminal_nodes": ["answer", "clarify"],
        "nodes": [
            {"node_id": "decide", "kind": "decision"},
            {"node_id": "answer", "kind": "answer"},
            {"node_id": "clarify", "kind": "clarify"},
        ],
        "edges": [
            {
                "from_node": "decide",
                "to_node": "answer",
                "condition": 'decision.action == "ANSWER"',
            },
            {
                "from_node": "decide",
                "to_node": "clarify",
                "condition": 'decision.action == "RETRIEVE"',
            },
        ],
    }

    plan = compile_snapshot(
        data, tenant_id="tenant-a", agent_id="review-agent", fallback_model="fallback"
    )

    assert plan.workflow_policy["adjacency"]["decide"][0]["condition"] == {
        "field": "decision.action",
        "operator": "==",
        "value": "ANSWER",
    }
