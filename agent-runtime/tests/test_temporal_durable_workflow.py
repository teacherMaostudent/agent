import asyncio

from agent_runtime_service.runtime.temporal_queue import AgentRunWorkflow


def test_durable_workflow_declares_approval_signal_and_resume_activity() -> None:
    """长期工作流必须显式拥有审批 Signal 和恢复 Activity，而非在等待时结束。"""
    workflow = AgentRunWorkflow()

    workflow.approve({"approved": True, "approval_id": "approval-1"})

    assert workflow._input == {"approved": True, "approval_id": "approval-1"}
    assert asyncio.iscoroutinefunction(AgentRunWorkflow.run)
