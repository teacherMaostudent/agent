"""Capabilities-first retrieval provider contracts.

The RAG coordinator depends on retrieval capabilities rather than a concrete
database client.  This keeps OpenSearch as the default implementation while a
future Milvus, pgvector, or graph adapter can be added without leaking its
index parameters into Runtime or Agent snapshots.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.contracts.rag import RagSearchRequest
from app.domain.models import RetrievalCandidate


@dataclass(frozen=True)
class RetrievalCapabilities:
    """Explicit backend feature declaration used by profile compilation and health checks."""

    dense: bool
    lexical: bool
    graph: bool = False
    scalar_acl_filter: bool = True
    immutable_index_versions: bool = True


class RetrievalProvider(Protocol):
    """A provider returns untrusted candidates only; it must never mint Evidence."""

    capabilities: RetrievalCapabilities

    def retrieve(self, request: RagSearchRequest) -> list[RetrievalCandidate]:
        """Return ACL-prefiltered candidates with channel and ranking lineage."""
        ...
