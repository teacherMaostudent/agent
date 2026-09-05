"""Permission-scoped semantic cache for already verified RAG responses.

The cache is deliberately *after* EvidenceVerifier.  It stores no unverified
candidate and its partition key includes every fact that could make a cached
answer unsafe: tenant, subject, authorization digest, index build, embedding,
retrieval profile and reranker revision.  A cache miss is always safe; a cache
hit is allowed only inside that same immutable retrieval boundary.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from dataclasses import dataclass
from threading import RLock
from typing import Protocol

from app.contracts.rag import RagSearchRequest, RagSearchResponse


class QueryEmbedder(Protocol):
    """Minimal query embedding surface; cache never owns an embedding provider."""

    def embed(self, text: str) -> list[float]:
        """Embed text in the same immutable vector space as the active index."""
        ...


@dataclass(frozen=True)
class _CacheEntry:
    """One short-lived, verified response associated with its normalized query vector."""

    normalized_query: str
    vector: tuple[float, ...]
    response_json: str
    expires_at_epoch: float


class SemanticResponseCache:
    """Bounded cache that permits only authorization- and version-safe reuse.

    ``redis`` persists the small partition bucket across query replicas.  The
    implementation intentionally does not use Redis Vector/RediSearch: that is
    an optional scale optimization, not a security dependency.  A bucket has a
    fixed maximum size, so a hot authorization scope cannot turn Redis into an
    unbounded secondary knowledge store.
    """

    def __init__(
        self,
        *,
        backend: str = "disabled",
        redis_url: str = "",
        ttl_seconds: int = 60,
        similarity_threshold: float = 0.985,
        max_entries_per_partition: int = 24,
    ) -> None:
        """Create a cache backend; unavailable Redis fails closed to cache misses."""
        self._backend = backend.lower()
        self._ttl_seconds = ttl_seconds
        self._similarity_threshold = similarity_threshold
        self._max_entries = max_entries_per_partition
        self._memory: dict[str, list[_CacheEntry]] = {}
        self._lock = RLock()
        self._redis = None
        if self._backend == "redis":
            try:
                import redis

                self._redis = redis.Redis.from_url(redis_url, decode_responses=True)
            except Exception:
                # A cache outage must never make retrieval unavailable or
                # silently fall back to an unsafe global cache.
                self._backend = "disabled"

    @property
    def enabled(self) -> bool:
        """Expose whether a configured cache may serve responses."""
        return self._backend in {"memory", "redis"}

    def get(
        self,
        request: RagSearchRequest,
        *,
        normalized_query: str,
        embedder: QueryEmbedder,
    ) -> RagSearchResponse | None:
        """Return a semantically equivalent verified response or a safe miss.

        Requests without an authorization scope digest *or a frozen index
        manifest* are deliberately never cached.  This prevents a legacy
        caller from receiving evidence after permission or index changes merely
        because it supplied the same user id.
        """
        partition = self._partition(request)
        if partition is None:
            return None
        try:
            query_vector = tuple(embedder.embed(normalized_query))
        except Exception:
            # Embedding is an optimization here. The authoritative retriever
            # will make its own request and retain its normal error semantics.
            return None
        if not query_vector:
            return None
        entries = self._load(partition)
        now = time.time()
        best: _CacheEntry | None = None
        best_similarity = self._similarity_threshold
        for entry in entries:
            if entry.expires_at_epoch <= now:
                continue
            similarity = self._cosine(query_vector, entry.vector)
            if similarity >= best_similarity:
                best, best_similarity = entry, similarity
        if best is None:
            return None
        response = RagSearchResponse.model_validate_json(best.response_json)
        return response.model_copy(
            update={"query": request.query, "cache_status": "HIT_SEMANTIC"}, deep=True
        )

    def put(
        self,
        request: RagSearchRequest,
        *,
        normalized_query: str,
        response: RagSearchResponse,
        embedder: QueryEmbedder,
    ) -> None:
        """Store only a completed verified response, bounded by evidence freshness."""
        partition = self._partition(request)
        if partition is None:
            return
        try:
            vector = tuple(embedder.embed(normalized_query))
        except Exception:
            return
        if not vector:
            return
        now = time.time()
        entry = _CacheEntry(
            normalized_query=normalized_query,
            vector=vector,
            response_json=response.model_copy(
                update={"cache_status": "MISS"}, deep=True
            ).model_dump_json(),
            expires_at_epoch=now + self._ttl_seconds,
        )
        current = [item for item in self._load(partition) if item.expires_at_epoch > now]
        # Replace exact normalized-query entries.  Semantically similar but
        # non-identical queries remain separate evidence audits.
        current = [item for item in current if item.normalized_query != normalized_query]
        current.append(entry)
        self._save(partition, current[-self._max_entries :])

    def _partition(self, request: RagSearchRequest) -> str | None:
        """Hash all access and retrieval identities that must not cross cache lines."""
        if (
            not self.enabled
            or not request.authorization_scope_digest
            or not request.index_manifest_id
            or request.content
        ):
            return None
        values = {
            "tenant": request.tenant_id,
            "user": request.user_id,
            "scope": request.authorization_scope_digest,
            "document": request.document_id or "",
            "index": request.index_version,
            "manifest": request.index_manifest_id,
            "embedding": request.embedding_contract_id,
            "profile": request.retrieval_profile,
            "profile_revision": request.retrieval_profile_revision,
            "reranker": request.reranker_contract_id,
        }
        encoded = json.dumps(values, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return f"rag:semantic-cache:{hashlib.sha256(encoded).hexdigest()}"

    def _load(self, partition: str) -> list[_CacheEntry]:
        """Load a partition without allowing cache backend failures into the query path."""
        try:
            if self._backend == "redis" and self._redis is not None:
                encoded = self._redis.get(partition)
                return self._decode(encoded) if encoded else []
            if self._backend == "memory":
                with self._lock:
                    return list(self._memory.get(partition, []))
        except Exception:
            return []
        return []

    def _save(self, partition: str, entries: list[_CacheEntry]) -> None:
        """Persist a bounded bucket; loss on contention is a performance-only outcome."""
        try:
            if self._backend == "redis" and self._redis is not None:
                self._redis.setex(partition, self._ttl_seconds, self._encode(entries))
            elif self._backend == "memory":
                with self._lock:
                    self._memory[partition] = list(entries)
        except Exception:
            # Retrieval is authoritative; caching must be invisible when Redis
            # is unavailable, so no exception is propagated from this branch.
            return

    @staticmethod
    def _encode(entries: list[_CacheEntry]) -> str:
        """Convert cache entries to an explicit JSON payload safe for Redis strings."""
        return json.dumps(
            [
                {
                    "query": item.normalized_query,
                    "vector": item.vector,
                    "response": item.response_json,
                    "expires": item.expires_at_epoch,
                }
                for item in entries
            ],
            separators=(",", ":"),
        )

    @staticmethod
    def _decode(encoded: str) -> list[_CacheEntry]:
        """Reject malformed external cache data rather than attempting partial reuse."""
        try:
            values = json.loads(encoded)
            if not isinstance(values, list):
                return []
            return [
                _CacheEntry(
                    normalized_query=str(item["query"]),
                    vector=tuple(float(value) for value in item["vector"]),
                    response_json=str(item["response"]),
                    expires_at_epoch=float(item["expires"]),
                )
                for item in values
            ]
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return []

    @staticmethod
    def _cosine(left: tuple[float, ...], right: tuple[float, ...]) -> float:
        """Compute a dimension-safe cosine similarity for normalized query vectors."""
        if len(left) != len(right):
            return -1.0
        denominator = math.sqrt(sum(item * item for item in left)) * math.sqrt(
            sum(item * item for item in right)
        )
        return sum(a * b for a, b in zip(left, right, strict=True)) / denominator if denominator else 0.0
