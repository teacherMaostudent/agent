"""Disposable OpenSearch projection guarded by tenant and user ACL filters.

The projection accelerates retrieval only.  Source documents remain in the
authoritative repository/object store so an index can be rebuilt or versioned
without changing published knowledge history.
"""

from __future__ import annotations

import hashlib
from typing import Any

import httpx

from app.contracts.rag import RagSearchRequest, RagSearchResponse
from app.domain.models import Chunk, Document, Evidence
from app.retrieval.vector_retriever import HashEmbeddingRetriever


class NullSearchProjection:
    def index_document(self, document: Document, chunks: list[Chunk]) -> None:
        """Perform index document within the NullSearchProjection ownership boundary."""
        del document, chunks


class OpenSearchProjection:
    """Rebuildable OpenSearch projection; PostgreSQL/S3 remain authoritative."""

    def __init__(self, settings) -> None:
        """Initialize OpenSearchProjection dependencies and local state."""
        self.url = settings.opensearch_url.rstrip("/")
        self.alias = settings.opensearch_index_alias
        self.version = settings.opensearch_index_version
        self.index = f"{self.alias}-{self.version}"
        self.dim = settings.local_embedding_dim
        self.auth = (
            (settings.opensearch_username, settings.opensearch_password)
            if settings.opensearch_username
            else None
        )
        self.embedder = HashEmbeddingRetriever(self.dim)
        self._ensure_index()

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        """Internal helper for OpenSearchProjection; preserve its caller-facing invariant."""
        response = httpx.request(
            method,
            f"{self.url}/{path.lstrip('/')}",
            auth=self.auth,
            timeout=30,
            **kwargs,
        )
        if response.status_code not in {200, 201}:
            response.raise_for_status()
        return response

    def _ensure_index(self) -> None:
        """Internal helper for OpenSearchProjection; preserve its caller-facing invariant."""
        exists = httpx.head(f"{self.url}/{self.index}", auth=self.auth, timeout=10)
        if exists.status_code == 404:
            self._request(
                "PUT",
                self.index,
                json={
                    "settings": {"index.knn": True},
                    "mappings": {
                        "dynamic": "strict",
                        "properties": {
                            "chunk_id": {"type": "keyword"},
                            "source_id": {"type": "keyword"},
                            "source_type": {"type": "keyword"},
                            "tenant_id": {"type": "keyword"},
                            "allowed_users": {"type": "keyword"},
                            "text": {"type": "text"},
                            "embedding": {
                                "type": "knn_vector",
                                "dimension": self.dim,
                                "method": {"name": "hnsw", "space_type": "cosinesimil"},
                            },
                            "metadata": {"type": "object", "enabled": False},
                        },
                    },
                },
            )
        alias = httpx.head(f"{self.url}/_alias/{self.alias}", auth=self.auth, timeout=10)
        if alias.status_code == 404:
            self.publish()

    def publish(self) -> None:
        # Alias swapping gives readers an all-or-nothing index version change;
        # they never observe a partially rebuilt index as the active corpus.
        """Perform publish within the OpenSearchProjection ownership boundary."""
        current = httpx.get(f"{self.url}/_alias/{self.alias}", auth=self.auth, timeout=10)
        actions: list[dict[str, Any]] = []
        if current.status_code == 200:
            actions.extend(
                {"remove": {"index": index, "alias": self.alias}} for index in current.json()
            )
        actions.append({"add": {"index": self.index, "alias": self.alias}})
        self._request("POST", "_aliases", json={"actions": actions})

    def index_document(self, document: Document, chunks: list[Chunk]) -> None:
        """Perform index document within the OpenSearchProjection ownership boundary."""
        tenant_id = str(document.metadata.get("tenant_id", ""))
        if not tenant_id:
            raise ValueError("indexed documents require tenant_id")
        allowed_users = document.metadata.get("allowed_users") or []
        for chunk in chunks:
            identifier = hashlib.sha256(f"{tenant_id}:{chunk.chunk_id}".encode()).hexdigest()
            self._request(
                "PUT",
                f"{self.index}/_doc/{identifier}",
                json={
                    "chunk_id": chunk.chunk_id,
                    "source_id": chunk.source_id,
                    "source_type": chunk.source_type,
                    "tenant_id": tenant_id,
                    "allowed_users": allowed_users,
                    "text": chunk.text,
                    "embedding": self.embedder._embed(chunk.text),
                    "metadata": chunk.metadata,
                },
            )

    def search(self, request: RagSearchRequest) -> RagSearchResponse:
        # ACL constraints are filters, not post-processing.  Unauthorized
        # chunks must not influence scores or candidate counts at all.
        """Perform search within the OpenSearchProjection ownership boundary."""
        acl_filter = [
            {"term": {"tenant_id": request.tenant_id}},
            {
                "bool": {
                    "should": [
                        {"term": {"allowed_users": request.user_id}},
                        {"bool": {"must_not": {"exists": {"field": "allowed_users"}}}},
                    ],
                    "minimum_should_match": 1,
                }
            },
        ]
        body = {
            "size": request.top_k,
            "query": {
                "script_score": {
                    "query": {
                        "bool": {
                            "filter": acl_filter,
                            "should": [{"match": {"text": request.query}}],
                            "minimum_should_match": 0,
                        }
                    },
                    "script": {
                        "source": "0.55 * _score + 0.45 * (cosineSimilarity(params.q, 'embedding') + 1.0)",
                        "params": {"q": self.embedder._embed(request.query)},
                    },
                }
            },
        }
        response = self._request("POST", f"{self.alias}/_search", json=body).json()
        hits = response.get("hits", {})
        evidence = [
            Evidence(
                source_id=item["_source"]["source_id"],
                source_type=item["_source"]["source_type"],
                text=item["_source"]["text"],
                score=float(item.get("_score", 0)),
                metadata=item["_source"].get("metadata", {}),
            )
            for item in hits.get("hits", [])
        ]
        total = hits.get("total", 0)
        candidate_count = int(total.get("value", 0) if isinstance(total, dict) else total)
        return RagSearchResponse(
            query=request.query,
            evidence=evidence,
            candidate_count=candidate_count,
            index_version=self.version,
        )


def build_search_projection(settings):
    """Perform build search projection within the module ownership boundary."""
    if settings.search_backend == "opensearch":
        return OpenSearchProjection(settings)
    return NullSearchProjection()
