"""Security regression tests for permission-scoped semantic response reuse."""

from app.contracts.rag import RagSearchRequest
from app.domain.models import RetrievalCandidate, RetrievalChannel
from app.rag.query_service import RagQueryService
from app.retrieval.semantic_cache import SemanticResponseCache


class _Embedder:
    """Small deterministic vector provider for cache behavior tests."""

    def embed(self, text: str) -> list[float]:
        """Map a normalized test query into a stable two-dimensional vector."""
        return [float(len(text)), 1.0]


class _Projection:
    """Counts authoritative searches so a cache hit is externally observable."""

    def __init__(self) -> None:
        self.calls = 0

    def search(self, request):
        """Return one tenant-owned candidate from the simulated query backend."""
        self.calls += 1
        return type(
            "Result",
            (),
            {
                "candidates": [
                    RetrievalCandidate(
                        chunk_id="chunk-1",
                        source_id="doc-1",
                        source_type="document",
                        text="approved evidence",
                        channel=RetrievalChannel.DENSE,
                        metadata={"tenant_id": request.tenant_id},
                    )
                ]
            },
        )()


class _Repository:
    """Provides the READY manifest required before a cached response is considered."""

    def get_index_manifest(self, manifest_id: str):
        """Return a minimal immutable manifest matching the test query boundary."""
        return (
            type(
                "Manifest",
                (),
                {"status": "READY", "tenant_id": "tenant-a", "index_version": "idx-1"},
            )()
            if manifest_id == "manifest-1"
            else None
        )


def _request(*, user_id: str = "user-a", scope: str = "scope-a") -> RagSearchRequest:
    """Build a fully pinned request eligible for semantic cache reuse."""
    return RagSearchRequest(
        query="approved policy",
        tenant_id="tenant-a",
        user_id=user_id,
        authorization_scope_digest=scope,
        index_version="idx-1",
        index_manifest_id="manifest-1",
        retrieval_profile="FAST",
        retrieval_profile_revision="retrieval-profile/v1",
        metadata={"allowed_retrieval_profiles": ["FAST"]},
    )


def test_semantic_cache_reuses_only_the_same_authorization_boundary() -> None:
    """A same-scope hit saves backend work; a different user must always miss."""
    projection = _Projection()
    service = RagQueryService(
        _Repository(),
        object(),
        search_projection=projection,
        index_version="idx-1",
        semantic_cache=SemanticResponseCache(backend="memory"),
        query_embedder=_Embedder(),
    )

    first = service.search(_request())
    second = service.search(_request())
    other_user = service.search(_request(user_id="user-b", scope="scope-b"))

    assert first.cache_status == "MISS"
    assert second.cache_status == "HIT_SEMANTIC"
    assert other_user.cache_status == "MISS"
    assert projection.calls == 2


def test_semantic_cache_bypasses_legacy_requests_without_authorization_digest() -> None:
    """Legacy calls cannot activate a cache merely by carrying a user id."""
    projection = _Projection()
    service = RagQueryService(
        _Repository(),
        object(),
        search_projection=projection,
        index_version="idx-1",
        semantic_cache=SemanticResponseCache(backend="memory"),
        query_embedder=_Embedder(),
    )

    request = _request(scope="")
    first = service.search(request)
    second = service.search(request)

    assert first.cache_status == "MISS"
    assert second.cache_status == "MISS"
    assert projection.calls == 2


def test_cache_embedding_failure_is_a_safe_miss() -> None:
    """A cache-only embedding failure must not alter the authoritative RAG result."""
    class BrokenEmbedder:
        def embed(self, text: str) -> list[float]:
            """Simulate a transient cache-vector dependency failure."""
            del text
            raise RuntimeError("cache embedding unavailable")

    projection = _Projection()
    service = RagQueryService(
        _Repository(),
        object(),
        search_projection=projection,
        index_version="idx-1",
        semantic_cache=SemanticResponseCache(backend="memory"),
        query_embedder=BrokenEmbedder(),
    )

    response = service.search(_request())

    assert response.evidence[0].source_id == "doc-1"
    assert projection.calls == 1
