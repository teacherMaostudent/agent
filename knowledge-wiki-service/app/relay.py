"""Transactional Outbox relay for RAG reindexing, evaluation gates and audit facts."""

import time
from datetime import UTC, datetime

import httpx
from platform_infra.identity import build_workload_token_provider

from app.config import Settings
from app.models import OutboxEvent
from app.repository import WikiRepository


class WikiRelay:
    """Deliver committed Wiki side effects without participating in the approval transaction."""

    def __init__(self, settings: Settings, repository: WikiRepository) -> None:
        """Create a dedicated workload token provider and optional mTLS connection pool."""
        self.settings = settings
        self.repository = repository
        self.workload_identity = build_workload_token_provider(settings)
        client_options: dict = {"timeout": 20}
        if settings.mtls_enabled:
            client_options.update(
                verify=settings.mtls_ca_file,
                cert=(settings.mtls_cert_file, settings.mtls_key_file),
            )
        self.client = httpx.Client(**client_options)

    def _post(self, url: str, **kwargs) -> httpx.Response:
        """Send one downstream request with the relay workload identity and mTLS transport."""
        headers = dict(kwargs.pop("headers", {}))
        headers.update(self.workload_identity.authorization_header())
        return self.client.post(url, headers=headers, **kwargs)

    def run_once(self) -> int:
        """Lease one bounded batch, deliver each event and persist success/retry/DLQ outcome."""
        delivered = 0
        for event in self.repository.claim_events(
            self.settings.relay_batch_size, self.settings.relay_lease_seconds
        ):
            try:
                if event.event_type == "wiki.rag.reindex.requested":
                    response = self._post(
                        f"{self.settings.ingestion_base_url.rstrip('/')}/ingestion/wiki-pages",
                        headers={
                            "X-Tenant-Id": event.tenant_id,
                            "X-User-Id": "knowledge-wiki-relay",
                            **(
                                {"X-Rag-Agent-Key": self.settings.ingestion_service_key}
                                if self.settings.ingestion_service_key else {}
                            ),
                        },
                        json={
                            "page_id": event.payload["page_id"],
                            "candidate_id": event.payload["candidate_id"],
                            "version": event.payload["version"],
                            "title": event.payload["title"],
                            "markdown": event.payload["markdown"],
                            "content_sha256": event.payload["content_sha256"],
                            "approved_by": event.payload["approved_by"],
                            "source_ids": event.payload["source_ids"],
                            "valid_until": event.payload.get("valid_until"),
                            "supersedes_page_ids": event.payload.get("supersedes_page_ids", []),
                        },
                        timeout=20,
                    )
                    response.raise_for_status()
                    self.repository.save_reindex_receipt(
                        event.tenant_id,
                        event.payload["page_id"],
                        int(event.payload["version"]),
                        str(response.json().get("job_id", "")),
                    )
                elif event.event_type == "wiki.release_gate.requested":
                    job_id = self.repository.reindex_job_id(
                        event.tenant_id,
                        event.payload["page_id"],
                        int(event.payload["version"]),
                    )
                    if not job_id:
                        raise ValueError(
                            "RAG ingestion receipt is missing; release gate remains blocked"
                        )
                    response = self._post(
                        f"{self.settings.governance_base_url.rstrip('/')}/v1/governance/evaluations/knowledge-change-gates",
                        headers=self._auditor_headers(event),
                        json={"pageId": event.payload["page_id"],
                              "contentSha256": event.payload["content_sha256"],
                              "version": event.payload["version"], "reindexJobId": job_id},
                        timeout=20,
                    )
                    response.raise_for_status()
                else:
                    response = self._post(
                        f"{self.settings.governance_base_url.rstrip('/')}/v1/governance/events",
                        headers={"X-Governance-Event-Key": self.settings.governance_event_key},
                        json={"schema_version": "1.0", "event_id": event.event_id,
                              "source_service": "knowledge-wiki-service",
                              "event_type": event.event_type,
                              "trace_id": event.payload.get("candidate_id", event.event_id),
                              "tenant_id": event.tenant_id,
                              "occurred_at": datetime.now(UTC).isoformat(),
                              "payload": event.payload}, timeout=20,
                    )
                    response.raise_for_status()
                self.repository.mark_event(event, True)
                delivered += 1
            except (httpx.HTTPError, ValueError, KeyError) as exc:
                self.repository.mark_event(
                    event,
                    False,
                    error=f"{type(exc).__name__}: {exc}",
                    max_attempts=self.settings.relay_max_attempts,
                    max_backoff_seconds=self.settings.relay_max_backoff_seconds,
                )
        return delivered

    def close(self) -> None:
        """Release the relay's mTLS HTTP connection pool during process shutdown."""
        self.client.close()

    def _auditor_headers(self, event: OutboxEvent) -> dict[str, str]:
        """Attach delegated tenant context and the least-privilege Governance auditor role."""
        return {"X-Tenant-Id": event.tenant_id, "X-User-Id": "knowledge-wiki-relay",
                "X-Roles": "governance-auditor",
                "X-Governance-Auditor-Key": self.settings.governance_auditor_key}


def main() -> None:
    """Run the independent relay process until shutdown while always releasing its resources."""
    settings = Settings()
    repository = WikiRepository(settings.database_url)
    relay = WikiRelay(settings, repository)
    try:
        while True:
            relay.run_once()
            time.sleep(settings.relay_poll_seconds)
    finally:
        relay.close()
        repository.close()
