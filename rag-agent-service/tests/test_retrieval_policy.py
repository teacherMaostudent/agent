from app.runtime.retrieval_policy import (
    RetrievalProfile,
    infer_profile,
    resolve_profile,
)


def test_explicit_profile_is_resolved_and_expanded() -> None:
    decision = infer_profile("review this", "general_question", {"retrievalProfile": "STRICT_EVIDENCE"})
    policy = resolve_profile(decision, snapshot={}, budget={"max_retrieval_rounds": 5})
    assert policy.profile is RetrievalProfile.STRICT_EVIDENCE
    assert policy.retrieval_required is True
    assert policy.citation_required is True
    assert policy.allow_answer_without_evidence is False
    assert policy.evidence_top_k == 5


def test_disallowed_profile_is_downgraded_by_published_policy() -> None:
    decision = infer_profile("deep research", "general_question", {})
    policy = resolve_profile(
        decision,
        snapshot={"spec": {"retrieval_policy": {"allowed_profiles": ["STANDARD"], "default_profile": "STANDARD"}}},
        budget={"max_retrieval_rounds": 3},
    )
    assert policy.profile is RetrievalProfile.STANDARD
    assert policy.decision == "DOWNGRADED"
    assert policy.reason == "PROFILE_NOT_ALLOWED"


def test_no_rag_never_calls_retrieval_rounds() -> None:
    decision = infer_profile("仅根据上文总结", "general_question", {})
    policy = resolve_profile(decision, snapshot={}, budget={"max_retrieval_rounds": 3})
    assert policy.profile is RetrievalProfile.NO_RAG
    assert policy.max_rounds == 0
    assert policy.allow_answer_without_evidence is True
