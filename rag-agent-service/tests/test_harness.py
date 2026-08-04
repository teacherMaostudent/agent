from app.runtime.catalog import AgentCatalog
from app.runtime.harness import AgentHarness, CallableExecutor


class _Graph:
    def run(self, initial, thread_id):
        return ("run", initial.get("agent_id"), thread_id)

    def resume(self, thread_id, approval, *, max_steps):
        return ("resume", thread_id, max_steps)


def test_harness_resolves_registered_agent_without_changing_default() -> None:
    default = _Graph()
    compliance = _Graph()
    harness = AgentHarness(default, registry={"compliance-agent": compliance})

    assert harness._resolve({"agent_id": "compliance-agent"}) is compliance
    assert harness._resolve({"agent_id": "unknown-agent"}) is default
    assert harness.registered_agents == ("compliance-agent",)


def test_harness_rejects_duplicate_or_empty_registration() -> None:
    graph = _Graph()
    harness = AgentHarness(graph)
    harness.register("sales-agent", graph)

    try:
        harness.register("sales-agent", graph)
    except ValueError as exc:
        assert "already registered" in str(exc)
    else:
        raise AssertionError("duplicate registration should fail")

    try:
        harness.register("", graph)
    except ValueError as exc:
        assert "must not be empty" in str(exc)
    else:
        raise AssertionError("empty registration should fail")


def test_harness_supports_callable_executor_and_catalog() -> None:
    default = _Graph()
    harness = AgentHarness(default)
    catalog = AgentCatalog()
    catalog.register("sales-agent", lambda snapshot: CallableExecutor(lambda initial, thread: ("sales", thread)))
    harness.register_from_catalog("sales-agent", {"agent_version": "v1"}, catalog)
    assert harness.run({"agent_id": "sales-agent"}, "t-1") == ("sales", "t-1")
