"""Authenticated client for the stable knowledge-ingestion API."""

from __future__ import annotations

import httpx
from platform_infra.identity import WorkloadTokenProvider

from platform_sdk.contracts.ingestion import (
    ApprovedArtifactIngestion,
    ArtifactIngestionReceipt,
)


class IngestionClient:
    """Submit approved immutable artifacts without importing the RAG application package."""

    def __init__(
        self,
        base_url: str,
        service_api_key: str = "",
        timeout: float = 30,
        workload_identity: WorkloadTokenProvider | None = None,
        *,
        mtls: dict | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.service_api_key = service_api_key
        self.workload_identity = workload_identity
        self.client = httpx.Client(timeout=timeout, **(mtls or {}))

    def submit_artifact(
        self,
        payload: ApprovedArtifactIngestion,
        *,
        tenant_id: str,
        user_id: str,
    ) -> ArtifactIngestionReceipt:
        """Create or return the idempotent document/job pair for one approved artifact."""
        headers = {
            "X-Tenant-Id": tenant_id,
            "X-User-Id": user_id,
            **({"X-Rag-Agent-Key": self.service_api_key} if self.service_api_key else {}),
            **(
                self.workload_identity.authorization_header()
                if self.workload_identity is not None
                else {}
            ),
        }
        response = self.client.post(
            f"{self.base_url}/api/v1/ingestion/artifacts",
            json=payload.model_dump(mode="json"),
            headers=headers,
            timeout=self.timeout,
        )
        response.raise_for_status()
        return ArtifactIngestionReceipt.model_validate(response.json())

    def get_job(self, job_id: str, *, tenant_id: str, user_id: str) -> dict:
        """Read the ingestion service's authoritative job state for UI projection only."""
        headers = {
            "X-Tenant-Id": tenant_id,
            "X-User-Id": user_id,
            **({"X-Rag-Agent-Key": self.service_api_key} if self.service_api_key else {}),
            **(
                self.workload_identity.authorization_header()
                if self.workload_identity is not None
                else {}
            ),
        }
        response = self.client.get(
            f"{self.base_url}/api/v1/ingestion/jobs/{job_id}",
            headers=headers,
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = response.json()
        return payload if isinstance(payload, dict) else {}

    def close(self) -> None:
        """Release the pooled HTTP connections owned by this workload."""
        self.client.close()
