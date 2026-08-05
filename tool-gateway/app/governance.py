"""Tool invocation audit publication with an outbox-style failure boundary.

Tool execution must not be retried merely because the audit sink is down.  The
publisher records the execution outcome locally first and treats downstream
governance delivery as an independently retryable concern.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import httpx
from platform_infra.identity import WorkloadTokenProvider

from app.domain.models import InvocationContext, InvocationResponse, ToolSpec
from app.infrastructure.repository import SqliteRepository


class GovernanceOutboxPublisher:
    """Durable adapter from Tool Gateway audit events to Agent Governance."""

    def __init__(
        self,
        repository: SqliteRepository,
        base_url: str,
        event_key: str,
        timeout: float,
        workload_identity: WorkloadTokenProvider | None = None,
        delivery_mode: str = "direct",
    ) -> None:
        """Initialize GovernanceOutboxPublisher dependencies and local state."""
        self.repository = repository
        self.base_url = base_url.rstrip("/")
        self.event_key = event_key
        self.timeout = timeout
        self.workload_identity = workload_identity
        self.delivery_mode = delivery_mode

    def publish_invocation(
        self,
        response: InvocationResponse,
        context: InvocationContext,
        spec: ToolSpec,
        approval_granted: bool,
    ) -> None:
        """Perform publish invocation within the GovernanceOutboxPublisher ownership boundary."""
        self.repository.enqueue_event(
            {
                "event_id": f"evt_{uuid4().hex}",
                "source_service": "tool-gateway",
                "event_type": "tool.execution.completed",
                "trace_id": context.trace_id or context.request_id,
                "tenant_id": context.tenant_id,
                "occurred_at": datetime.now(UTC).isoformat(),
                "payload": {
                    "request_id": context.request_id,
                    "run_id": context.run_id,
                    "session_id": context.session_id,
                    "agent_id": context.agent_id,
                    "agent_version": context.agent_version,
                    "snapshot_id": context.snapshot_id,
                    "tool_name": response.tool_name,
                    "tool_version": response.tool_version,
                    "invocation_id": response.invocation_id,
                    "status": response.status.value,
                    "attempt_count": response.attempt_count,
                    "duration_ms": response.duration_ms,
                    "risk": spec.risk.value,
                    "approval_required": spec.approval_required,
                    "approval_granted": approval_granted,
                },
            }
        )

    async def flush(self) -> None:
        """Perform flush within the GovernanceOutboxPublisher ownership boundary."""
        # CDC observes the committed outbox row.  Sending the same row over
        # HTTP would create a second transport and turn deduplication into a
        # correctness requirement rather than a safety net.
        if self.delivery_mode == "cdc" or not self.base_url:
            return
        headers = {"X-Governance-Event-Key": self.event_key} if self.event_key else {}
        if self.workload_identity is not None:
            headers.update(self.workload_identity.authorization_header())
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            for event in self.repository.pending_events():
                try:
                    response = await client.post(
                        f"{self.base_url}/v1/governance/events", json=event, headers=headers
                    )
                    response.raise_for_status()
                    self.repository.mark_event_delivered(event["event_id"])
                except httpx.HTTPError as exc:
                    self.repository.mark_event_failed(event["event_id"], str(exc))
