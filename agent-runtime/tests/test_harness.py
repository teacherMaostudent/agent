from agent_runtime_service.runtime.catalog import ExecutorCatalog
from agent_runtime_service.runtime.harness import (
    AgentHarness,
    CallableExecutor,
    DurableExecutor,
    SimpleExecutor,
)
from agent_runtime_service.runtime.snapshot_compiler import CompiledAgentPlan


def _plan(profile: str = "sales/v1") -> CompiledAgentPlan:
    return CompiledAgentPlan(
        contract_hash="hash",
        graph_id="graph",
        graph_entrypoint="start",
        graph_terminal_nodes=["end"],
        graph_execution_order=["start", "end"],
        graph_node_kinds={"start": "planner", "end": "finalize"},
        executor_profile=profile,
        prompt_template="{{task}}",
        logical_model="offline",
    )


def _harness(*, cancelled=None) -> AgentHarness:
    """构造只含已部署执行器的 Harness，模拟启动期目录装配。"""
    catalog = ExecutorCatalog(
        {
            "sales/v1": CallableExecutor(
                lambda initial, thread: ("sales", initial["agent_id"], thread),
                lambda thread, approval, *, max_steps: ("resume", thread, max_steps),
            )
        }
    )
    return AgentHarness(
        release_resolver=None,
        executor_resolver=catalog,
        fallback_model="offline",
        snapshot_required=False,
        cancel_execution=cancelled or (lambda tenant_id, run_id: {"run_id": run_id}),
    )


def test_harness_resolves_only_deployed_executor_profile() -> None:
    harness = _harness()

    assert harness.executor_profiles == ("sales/v1",)
    assert harness.resolve_executor(_plan()).run({"agent_id": "sales"}, "t-1") == (
        "sales",
        "sales",
        "t-1",
    )


def test_harness_rejects_unknown_profile_before_execution() -> None:
    harness = _harness()

    try:
        harness.resolve_executor(_plan("unknown/v1"))
    except LookupError as exc:
        assert "not deployed" in str(exc)
    else:
        raise AssertionError("Harness must reject an undeployed executor profile")


def test_harness_runs_resumes_and_delegates_cancel_without_business_logic() -> None:
    cancellations: list[tuple[str, str]] = []
    harness = _harness(cancelled=lambda tenant_id, run_id: cancellations.append((tenant_id, run_id)))
    state = {
        "agent_id": "sales",
        "compiled_plan": _plan().model_dump(mode="json"),
        "executor_profile": "sales/v1",
    }

    assert harness.run(state, "t-1", _plan()) == ("sales", "sales", "t-1")
    assert harness.resume("t-1", type("Approval", (), {})(), max_steps=5, plan=_plan()) == (
        "resume",
        "t-1",
        5,
    )
    assert harness.cancel("tenant-a", "run-a") is None
    assert cancellations == [("tenant-a", "run-a")]


def test_simple_and_durable_executor_profiles_have_distinct_execution_boundaries() -> None:
    """短任务不进入 Graph，长期任务只能由 Temporal Worker 标志触发。"""
    simple = SimpleExecutor()
    assert simple.run({"task": "return this"}, "thread").answer == "return this"

    durable = DurableExecutor(
        CallableExecutor(lambda initial, thread: ("graph", initial["task"], thread))
    )
    try:
        durable.run({"task": "long"}, "thread")
    except RuntimeError as exc:
        assert "asynchronous /runs" in str(exc)
    else:
        raise AssertionError("Durable Executor must reject synchronous API execution")
    assert durable.run({"task": "long", "temporal_worker_execution": True}, "thread") == (
        "graph",
        "long",
        "thread",
    )
