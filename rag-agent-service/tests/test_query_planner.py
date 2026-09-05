from app.domain.models import RetrievalCandidate, RetrievalChannel
from app.retrieval.profiles import resolve_retrieval_profile
from app.retrieval.query_planner import QueryPlanner, fuse_query_candidates


def test_query_planner_preserves_identifier_under_bounded_variant_budget() -> None:
    plan = QueryPlanner().plan("  Find SOP-2026-017 retention  ", max_variants=2)

    assert plan.queries == ["Find SOP-2026-017 retention", "SOP-2026-017"]
    assert "EXACT_IDENTIFIER_RECALL" in plan.reason_codes


def test_query_candidate_fusion_keeps_variant_lineage() -> None:
    shared = RetrievalCandidate(
        chunk_id="chunk-a",
        source_id="doc-a",
        source_type="policy",
        text="retention",
        channel=RetrievalChannel.HYBRID,
    )
    merged = fuse_query_candidates([[shared], [shared]])

    assert len(merged) == 1
    assert merged[0].metadata["query_fusion"] == "RRF"
    assert merged[0].metadata["query_variant_indexes"] == [0, 1]


def test_enterprise_evidence_profile_bounds_broad_recall_and_compact_projection() -> None:
    """High-assurance workflows opt into 100 candidates and only 10 evidence items."""
    profile = resolve_retrieval_profile("ENTERPRISE_EVIDENCE")

    assert profile.candidate_top_k == 100
    assert profile.max_rerank_docs == 100
    assert profile.evidence_top_k == 10
