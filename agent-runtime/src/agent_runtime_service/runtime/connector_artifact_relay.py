"""Independent relay for Desktop Connector result artifacts.

The desktop side effect and Tool Gateway receipt are already durable before this relay runs.
Consequently, a Context outage is retried from Runtime's outbox and never asks the desktop to
repeat the local action.  Lease tokens make multiple relay replicas safe to run concurrently.
"""

from __future__ import annotations

import asyncio
import json
import logging
import signal
from contextlib import suppress
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import uuid4

if TYPE_CHECKING:
    from agent_runtime_service.runtime.container import AgentRuntimeContainer

LOGGER = logging.getLogger(__name__)


class ConnectorArtifactRelay:
    """Deliver claimed outbox records to Context with bounded retries and DLQ transition."""

    def __init__(self, container: AgentRuntimeContainer) -> None:
        self.container = container
        self.settings = container.settings

    def run_once(self, *, tenant_id: str | None = None, limit: int | None = None) -> dict[str, int]:
        """Process one finite batch and return counters suitable for metrics or an admin API."""
        disconnected = self.container.run_store.reconcile_stale_connectors(
            self.settings.connector_heartbeat_timeout_seconds
        )
        claimed = self.container.run_store.claim_connector_artifacts(
            tenant_id=tenant_id,
            limit=limit or self.settings.connector_artifact_relay_batch_size,
            lease_seconds=self.settings.connector_artifact_relay_lease_seconds,
        )
        delivered = retried = dead_lettered = lease_lost = 0
        for item in claimed:
            outbox_id = str(item["outbox_id"])
            lease_token = str(item["lease_token"])
            try:
                content = item["content_json"]
                if not isinstance(content, str):
                    content = json.dumps(content, ensure_ascii=False)
                artifact = self.container.runtime_context.context.create_text_artifact(
                    str(item["root_task_id"]),
                    content,
                    tenant_id=str(item["tenant_id"]),
                    user_id=str(item["user_id"]),
                )
                if self.container.run_store.mark_connector_artifact_delivered(
                    outbox_id, lease_token, str(artifact.artifact_id)
                ):
                    delivered += 1
                else:
                    lease_lost += 1
            except Exception as exc:
                state = self.container.run_store.fail_connector_artifact_delivery(
                    outbox_id,
                    lease_token,
                    type(exc).__name__,
                    max_attempts=self.settings.connector_artifact_relay_max_attempts,
                    max_backoff_seconds=self.settings.connector_artifact_relay_max_backoff_seconds,
                )
                if state == "DEAD_LETTER":
                    dead_lettered += 1
                    self.container.run_store.enqueue_governance(
                        {
                            "event_id": f"evt_{uuid4().hex}",
                            "source_service": "agent-runtime",
                            "event_type": "connector.artifact.dead_lettered",
                            "trace_id": "",
                            "tenant_id": str(item["tenant_id"]),
                            "occurred_at": datetime.now(UTC).isoformat(),
                            "payload": {
                                "outbox_id": outbox_id,
                                "task_id": str(item["task_id"]),
                                "attempts": self.settings.connector_artifact_relay_max_attempts,
                                "error_class": type(exc).__name__,
                            },
                        }
                    )
                    LOGGER.error(
                        "connector artifact entered DLQ",
                        extra={"outbox_id": outbox_id, "task_id": item["task_id"]},
                    )
                elif state == "LEASE_LOST":
                    lease_lost += 1
                else:
                    retried += 1
        if claimed:
            # Direct mode makes a best-effort delivery; CDC mode intentionally leaves the
            # transaction rows for Kafka Connect as the sole transport owner.
            self.container.governance.flush()
        return {
            "disconnected": disconnected,
            "claimed": len(claimed),
            "delivered": delivered,
            "retried": retried,
            "dead_lettered": dead_lettered,
            "lease_lost": lease_lost,
        }


async def run_worker() -> None:
    """Poll continuously until SIGINT/SIGTERM, closing shared clients on every exit path."""
    # Import lazily so the relay abstraction can be imported by Runtime API tests without
    # making the execution store depend on the application's composition root.
    from agent_runtime_service.runtime.container import AgentRuntimeContainer

    container = AgentRuntimeContainer(build_async_queue=False)
    relay = ConnectorArtifactRelay(container)
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signal_name in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError):
            loop.add_signal_handler(signal_name, stopping.set)
    try:
        while not stopping.is_set():
            counters = await asyncio.to_thread(relay.run_once)
            if counters["claimed"]:
                LOGGER.info("connector artifact relay batch", extra=counters)
            try:
                await asyncio.wait_for(
                    stopping.wait(),
                    timeout=container.settings.connector_artifact_relay_poll_seconds,
                )
            except TimeoutError:
                continue
    finally:
        container.close()


def main() -> None:
    """Console entry point used by Compose/Kubernetes as an independent workload."""
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
