from agent_runtime_service.runtime.temporal_routing import TemporalTargetRouter


def test_region_worker_queue_and_failover_order() -> None:
    router = TemporalTargetRouter(
        "temporal-cn:7233",
        '{"us":"temporal-us:7233","eu":"temporal-eu:7233"}',
    )
    assert router.target_for("us") == "temporal-us:7233"
    assert router.task_queue_for("agent-runtime", "us") == "agent-runtime-us"
    assert router.candidates("us") == [
        "temporal-us:7233",
        "temporal-cn:7233",
        "temporal-eu:7233",
    ]
