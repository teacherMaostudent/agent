from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import httpx

from app.domain.models import InvocationContext, InvocationResponse
from app.infrastructure.repository import SqliteRepository


class GovernanceOutboxPublisher:
    """Durable adapter from Tool Gateway audit events to Agent Governance."""

    def __init__(self, repository: SqliteRepository, base_url: str, event_key: str, timeout: float) -> None:
        self.repository = repository
        self.base_url = base_url.rstrip("/")
        self.event_key = event_key
        self.timeout = timeout

    def publish_invocation(self, response: InvocationResponse, context: InvocationContext) -> None:
        self.repository.enqueue_event({
            "event_id": f"evt_{uuid4().hex}",
            "source_service": "tool-gateway",
            "event_type": "tool.invocation.completed",
            "trace_id": context.trace_id or context.request_id,
            "tenant_id": context.tenant_id,
            "occurred_at": datetime.now(UTC).isoformat(),
            "payload": {
                "request_id": context.request_id, "run_id": context.run_id,
                "session_id": context.session_id, "agent_id": context.agent_id,
                "agent_version": context.agent_version, "snapshot_id": context.snapshot_id,
                "tool_name": response.tool_name, "tool_version": response.tool_version,
                "invocation_id": response.invocation_id, "status": response.status.value,
                "attempt_count": response.attempt_count, "duration_ms": response.duration_ms,
            },
        })

    async def flush(self) -> None:
        if not self.base_url:
            return
        headers = {"X-Governance-Event-Key": self.event_key} if self.event_key else {}
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            for event in self.repository.pending_events():
                try:
                    response = await client.post(f"{self.base_url}/v1/governance/events", json=event, headers=headers)
                    response.raise_for_status()
                    self.repository.mark_event_delivered(event["event_id"])
                except httpx.HTTPError:
                    return
