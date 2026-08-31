"""Tests for per-Run selection among Snapshot-published model routes."""

import pytest

from agent_runtime_service.runtime.snapshot_compiler import CompiledAgentPlan
from agent_runtime_service.service_api.runtime_api import _plan_for_requested_model_route


def _plan() -> CompiledAgentPlan:
    """Build the smallest executable plan; route choice must not alter unrelated fields."""
    return CompiledAgentPlan(
        contract_hash="a" * 64,
        graph_id="graph",
        graph_entrypoint="answer",
        graph_terminal_nodes=["answer"],
        graph_execution_order=["answer"],
        graph_node_kinds={"answer": "answer"},
        executor_profile="declarative-langgraph/v1",
        prompt_template="Answer.",
        logical_model="deepseek-v4-flash",
    )


def _snapshot() -> dict:
    return {
        "spec": {
            "model_policy": {
                "default_route": "deepseek-v4-flash",
                "routes": [
                    {
                        "route_name": "deepseek-v4-flash",
                        "models": ["deepseek-v4-flash"],
                    },
                    {
                        "route_name": "claude-sonnet-4",
                        "models": ["claude-sonnet-4", "claude-3-5-haiku"],
                        "fallback_route": "deepseek-v4-flash",
                        "data_region": "global",
                    },
                ],
            }
        }
    }


def test_requested_route_is_selected_only_from_published_snapshot() -> None:
    """A valid UI route changes only logical model, fallback chain and its data region."""
    selected = _plan_for_requested_model_route(_snapshot(), _plan(), "claude-sonnet-4")

    assert selected.logical_model == "claude-sonnet-4"
    assert selected.fallback_models == ["claude-3-5-haiku", "deepseek-v4-flash"]
    assert selected.data_region == "global"
    assert selected.graph_id == "graph"


def test_unpublished_route_is_rejected_even_when_it_looks_like_a_provider_model() -> None:
    """The request cannot smuggle an arbitrary Gateway target through the model field."""
    with pytest.raises(ValueError, match="not published"):
        _plan_for_requested_model_route(_snapshot(), _plan(), "openai:gpt-secret-preview")
