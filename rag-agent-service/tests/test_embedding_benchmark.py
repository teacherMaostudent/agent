from app.core.config import Settings
from app.domain.models import Chunk
from app.evaluation.embedding_benchmark import EmbeddingBenchmarkRunner, GoldenRetrievalCase
from app.retrieval.embedder import HashEmbedder


def test_embedding_benchmark_reports_recall_latency_cost_and_compliance_facts() -> None:
    """候选 Provider 的 Promote 证据必须同时给出质量、性能、成本和部署边界。"""
    case = GoldenRetrievalCase(
        case_id="golden-1",
        query="find the retention policy",
        chunks=(
            Chunk(source_id="policy", source_type="document", text="retention policy requirement"),
            Chunk(source_id="other", source_type="document", text="unrelated warehouse note"),
        ),
        expected_source_ids=frozenset({"policy"}),
        criticality="high",
    )

    report = EmbeddingBenchmarkRunner((case,), top_k=1).run(
        HashEmbedder(32), estimated_cost_per_million_chars_usd=0.2
    )

    assert report.recall_at_k == 1.0
    assert report.false_negative_rate == 0.0
    assert report.p95_latency_ms >= 0
    assert report.deployment_mode == "local"
    assert report.license == "internal-test-only"


def test_production_rejects_hash_embedding_even_without_fallback() -> None:
    """生产索引必须明确使用语义 Provider，不能把默认 Hash 当作可用生产回退。"""
    import pytest

    with pytest.raises(ValueError, match="RAG_EMBEDDING_PROVIDER"):
        Settings(
            deployment_environment="production",
            persistence="postgres",
            database_url="postgresql://example/platform",
            temporal_enabled=True,
            object_storage_backend="s3",
            s3_bucket="documents",
            search_backend="opensearch",
            opensearch_url="http://opensearch:9200",
            oidc_enabled=True,
            oidc_issuer="https://issuer",
            oidc_jwks_url="https://issuer/jwks",
            workload_token_url="https://issuer/token",
            workload_client_secret="secret",
            opa_enabled=True,
            redis_url="redis://redis",
            governance_delivery_mode="cdc",
            require_service_auth=True,
            service_api_key="key",
            cors_origins=["https://console.example"],
            runtime_snapshot_required=True,
            allow_legacy_public_documents=False,
        )
