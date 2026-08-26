"""Workspace execution projection regression tests."""

from agent_web_bff.main import _workspace_detail_projection


def test_workspace_detail_explains_execution_without_leaking_tool_content() -> None:
    """Owners see versions, plan, model, tool, budget and events, but not raw scan lines."""
    run = {
        "run_id": "run_1",
        "user_id": "user-1",
        "agent_id": "general-agent",
        "snapshot_id": "av_1",
        "status": "COMPLETED",
        "context": {
            "agent_version": "general-agent:1.0.0",
            "graph_version": "graph:1",
            "model_policy_version": "model-policy:1",
        },
        "result": {
            "answer": "done",
            "execution_plan": {
                "plan_id": "plan_1",
                "plan_stage": "ADMITTED",
                "executor_profile": "declarative-langgraph/v1",
                "intent": {"name": "knowledge_query", "confidence": 0.9},
                "complexity": {"score": 20, "level": "simple"},
                "route": {"route": "direct", "fallback_chain": ["clarify"]},
                "admission_checks": [
                    {"check": "tool_scope", "passed": True, "reason": "allowed"}
                ],
                "allowed_tool_scope": ["controlled_scan"],
            },
            "observations": [
                {
                    "type": "retrieval",
                    "query": "safe query",
                    "result_count": 2,
                    "retrieval_profile": "STANDARD",
                },
                {
                    "type": "tool",
                    "tool": "controlled_scan",
                    "success": True,
                    "result": {
                        "scope": "workspace",
                        "matches": [{"line": "TOP SECRET raw source line"}],
                    },
                },
            ],
            "budget": {
                "max_cost_usd": 1.0,
                "spent_cost_usd": 0.01,
                "llm_calls": 1,
                "tool_calls": 1,
            },
            "context_summary": {
                "selected_history_count": 2,
                "status": {"rag_status": "available", "budget_report": {"requested_tokens": 100}},
            },
            "latency_ms": 123,
        },
    }
    events = [
        {
            "event_id": "evt_1",
            "sequence": 3,
            "event_type": "runtime.request_epoch.pinned",
            "occurred_at": "2026-08-26T00:00:00Z",
            "run_id": "run_1",
            "status": "RUNNING",
            "metadata": {
                "model_route": "deepseek-chat",
                "model_revision": "deepseek-chat-r1",
                "prompt_version": "prompt-v1",
                "rendered_prompt": "must never reach the browser",
            },
            "model_message": {"content": "private model response"},
        }
    ]

    detail = _workspace_detail_projection(
        run,
        user_id="user-1",
        artifacts=[],
        events=events,
        release={"release_id": "rel_1", "status": "active"},
    )

    execution = detail["execution"]
    assert execution["release"]["release_id"] == "rel_1"
    assert execution["snapshot"]["snapshot_id"] == "av_1"
    assert execution["plan"]["plan_id"] == "plan_1"
    assert execution["retrieval"][0]["result_count"] == 2
    assert execution["model"]["routes"] == ["deepseek-chat"]
    assert execution["tools"][0]["match_count"] == 1
    assert execution["budget"]["spent_cost_usd"] == 0.01
    assert execution["timeline"][0]["event_id"] == "evt_1"
    serialized = str(detail)
    assert "TOP SECRET" not in serialized
    assert "private model response" not in serialized
    assert "rendered_prompt" not in serialized


def test_terminal_workspace_run_has_no_control_actions() -> None:
    """A completed Run cannot advertise steering, approval, or cancellation."""
    detail = _workspace_detail_projection(
        {
            "run_id": "run_2",
            "user_id": "user-1",
            "agent_id": "general-agent",
            "snapshot_id": "av_2",
            "status": "COMPLETED",
            "result": {},
        },
        user_id="user-1",
        artifacts=[],
        events=[],
        release={},
    )
    assert detail["available_actions"] == []

