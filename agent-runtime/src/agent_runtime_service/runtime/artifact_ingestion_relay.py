"""Relay approved Desktop scan Artifacts into the knowledge-ingestion service."""

from __future__ import annotations

import asyncio
import logging
import signal
from contextlib import suppress

from platform_sdk.contracts.ingestion import ApprovedArtifactIngestion

from agent_runtime_service.runtime.session_events import RuntimeEventType

LOGGER = logging.getLogger(__name__)


class ArtifactIngestionRelay:
    """Submit approval-bound immutable artifacts with leases and bounded retries."""

    def __init__(self, container) -> None:
        self.container = container
        self.settings = container.settings

    def run_once(self) -> dict[str, int]:
        """Process one claimed batch; no Desktop action is ever repeated by this relay."""
        claimed = self.container.run_store.claim_artifact_ingestions(
            limit=self.settings.artifact_ingestion_relay_batch_size,
            lease_seconds=self.settings.artifact_ingestion_relay_lease_seconds,
        )
        submitted = retried = dead_lettered = lease_lost = 0
        for item in claimed:
            try:
                run = self.container.run_store.get(str(item["tenant_id"]), str(item["run_id"]))
                if run is None:
                    raise RuntimeError("source run is unavailable")
                artifacts = self.container.runtime_context.context.list_task_artifacts(
                    str(item["root_task_id"]), tenant_id=str(item["tenant_id"]), limit=200
                )
                artifact = next(
                    (value for value in artifacts if value.artifact_id == item["artifact_id"]), None
                )
                if artifact is None:
                    raise RuntimeError("approved artifact is unavailable")
                receipt = self.container.ingestion.submit_artifact(
                    ApprovedArtifactIngestion(
                        artifact_id=artifact.artifact_id,
                        root_task_id=artifact.root_task_id,
                        content_ref=artifact.content_ref,
                        content_sha256=artifact.content_sha256,
                        media_type=artifact.media_type,
                        logical_name=artifact.logical_name or artifact.artifact_type,
                        approval_id=str(item["request_id"]),
                        approved_by=str(item["approved_by"]),
                    ),
                    tenant_id=str(item["tenant_id"]),
                    user_id=str(item["approved_by"]),
                )
                if not self.container.run_store.complete_artifact_ingestion(
                    str(item["request_id"]),
                    str(item["lease_token"]),
                    receipt.document_id,
                    receipt.job_id,
                ):
                    lease_lost += 1
                    continue
                event = self.container.run_store.append_session_event(
                    run.context,
                    RuntimeEventType.ARTIFACT_INGESTION_SUBMITTED,
                    status=run.status,
                    metadata={
                        "artifact_id": artifact.artifact_id,
                        "document_id": receipt.document_id,
                        "ingestion_job_id": receipt.job_id,
                        "approval_id": str(item["request_id"]),
                    },
                )
                self.container.publish_session_event(event)
                self.container.run_store.enqueue_governance(
                    {
                        "event_id": f"gov_{event.event_id}",
                        "source_service": "agent-runtime",
                        "event_type": "artifact.ingestion.submitted",
                        "trace_id": run.context.trace_id,
                        "tenant_id": str(item["tenant_id"]),
                        "occurred_at": event.occurred_at.isoformat(),
                        "payload": {
                            "run_id": str(item["run_id"]),
                            "artifact_id": artifact.artifact_id,
                            "document_id": receipt.document_id,
                            "ingestion_job_id": receipt.job_id,
                            "approval_id": str(item["request_id"]),
                        },
                    }
                )
                submitted += 1
            except Exception as exc:
                outcome = self.container.run_store.fail_artifact_ingestion(
                    str(item["request_id"]),
                    str(item["lease_token"]),
                    f"{type(exc).__name__}: {exc}",
                    max_attempts=self.settings.artifact_ingestion_relay_max_attempts,
                )
                if outcome == "DLQ":
                    dead_lettered += 1
                elif outcome == "LEASE_LOST":
                    lease_lost += 1
                else:
                    retried += 1
                LOGGER.warning(
                    "approved artifact ingestion failed",
                    extra={"request_id": item["request_id"], "outcome": outcome},
                )
        if claimed:
            self.container.governance.flush()
        return {
            "claimed": len(claimed),
            "submitted": submitted,
            "retried": retried,
            "dead_lettered": dead_lettered,
            "lease_lost": lease_lost,
        }


async def run_worker() -> None:
    """Poll until process shutdown while always releasing shared clients and stores."""
    from agent_runtime_service.runtime.container import AgentRuntimeContainer

    container = AgentRuntimeContainer(build_async_queue=False)
    relay = ArtifactIngestionRelay(container)
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signal_name in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError):
            loop.add_signal_handler(signal_name, stopping.set)
    try:
        while not stopping.is_set():
            counters = await asyncio.to_thread(relay.run_once)
            if counters["claimed"]:
                LOGGER.info("artifact ingestion relay batch", extra=counters)
            try:
                await asyncio.wait_for(
                    stopping.wait(), timeout=container.settings.artifact_ingestion_relay_poll_seconds
                )
            except TimeoutError:
                continue
    finally:
        container.close()


def main() -> None:
    """Entry point for the independently scalable Runtime relay workload."""
    asyncio.run(run_worker())
