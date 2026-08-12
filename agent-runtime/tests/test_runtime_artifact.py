from agent_runtime_service.runtime.catalog import ExecutorCatalog
from agent_runtime_service.runtime.harness import AgentHarness, CallableExecutor


def _snapshot() -> dict:
    """构造与 Control Plane 发布快照相同的最小合法声明。"""
    return {
        "schema_version": "1.0",
        "tenant_id": "tenant-a",
        "agent_id": "agent-a",
        "agent_version": "agent-a:1.0.0",
        "graph_version": "graph:1",
        "prompt_version": "prompt:1",
        "knowledge_version": "kb:1",
        "tool_set_version": "tools:1",
        "model_policy_version": "models:1",
        "published_at": "2026-01-01T00:00:00Z",
        "spec": {
            "graph": {
                "graph_id": "graph",
                "entrypoint": "decide",
                "terminal_nodes": ["answer"],
                "nodes": [
                    {"node_id": "decide", "kind": "decision"},
                    {"node_id": "answer", "kind": "answer"},
                ],
                "edges": [{"from_node": "decide", "to_node": "answer"}],
            },
            "prompt": {"prompt_id": "prompt", "system_template": "{{task}}", "variables": ["task"]},
            "tools": [],
            "knowledge": [],
            "model_policy": {
                "policy_id": "models",
                "default_route": "primary",
                "routes": [{"route_name": "primary", "capability": "chat", "models": ["model"]}],
            },
            "runtime_executor": "declarative-langgraph/v1",
        },
    }


def test_production_harness_loads_frozen_artifact_without_runtime_compilation() -> None:
    """生产 Harness 拒绝缺 Artifact 的快照，并加载发布期冻结的计划。"""
    from platform_sdk.contracts.runtime_snapshot import compile_runtime_snapshot

    snapshot = _snapshot()
    artifact = compile_runtime_snapshot(snapshot, tenant_id="tenant-a", agent_id="agent-a")
    snapshot["runtime_artifact"] = artifact.model_dump(mode="json")
    catalog = ExecutorCatalog({"declarative-langgraph/v1": CallableExecutor(lambda *_: None)})
    harness = AgentHarness(
        release_resolver=None,
        executor_resolver=catalog,
        fallback_model="local",
        snapshot_required=True,
        cancel_execution=lambda *_: None,
    )

    loaded = harness.load_snapshot({"snapshot": snapshot, "version_id": "version-a"}, tenant_id="tenant-a", agent_id="agent-a")

    assert loaded.plan.executor_profile == "declarative-langgraph/v1"
    snapshot["runtime_artifact"] = None
    try:
        harness.load_snapshot({"snapshot": snapshot}, tenant_id="tenant-a", agent_id="agent-a")
    except RuntimeError as exc:
        assert "artifact" in str(exc)
    else:
        raise AssertionError("production Runtime must reject an artifact-less snapshot")


def test_frozen_artifact_detects_snapshot_tampering() -> None:
    """Artifact 的哈希必须覆盖发布快照，运行前篡改任一策略字段都会被拒绝。"""
    from platform_sdk.contracts.runtime_snapshot import (
        RuntimeSnapshotCompileError,
        compile_runtime_snapshot,
        load_runtime_snapshot_artifact,
    )

    snapshot = _snapshot()
    snapshot["runtime_artifact"] = compile_runtime_snapshot(
        snapshot, tenant_id="tenant-a", agent_id="agent-a"
    ).model_dump(mode="json")
    snapshot["spec"]["runtime_executor"] = "simple/v1"

    try:
        load_runtime_snapshot_artifact(snapshot, tenant_id="tenant-a", agent_id="agent-a")
    except RuntimeSnapshotCompileError as exc:
        assert "hash" in str(exc)
    else:
        raise AssertionError("Runtime must reject a snapshot whose artifact hash drifted")
