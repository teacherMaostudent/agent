"""Versioned, server-enforced retrieval profiles.

Runtime may choose an allowed profile, but it cannot supply arbitrary retrieval
limits.  The query plane resolves the selected profile again so a buggy or
untrusted client cannot turn a FAST release into an expensive broad search.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ResolvedRetrievalProfile:
    """The bounded query-plan fragment consumed by retrieval and evidence stages."""

    name: str
    revision: str
    candidate_top_k: int
    evidence_top_k: int
    rerank_enabled: bool
    max_query_variants: int
    max_rerank_docs: int


_PROFILES: dict[str, ResolvedRetrievalProfile] = {
    "NO_RAG": ResolvedRetrievalProfile("NO_RAG", "retrieval-profile/v1", 0, 0, False, 0, 0),
    "FAST": ResolvedRetrievalProfile("FAST", "retrieval-profile/v1", 12, 3, False, 1, 0),
    "STANDARD": ResolvedRetrievalProfile("STANDARD", "retrieval-profile/v1", 30, 5, True, 1, 30),
    "STRICT_EVIDENCE": ResolvedRetrievalProfile(
        "STRICT_EVIDENCE", "retrieval-profile/v1", 50, 5, True, 2, 50
    ),
    # The explicit enterprise profile mirrors the high-recall pipeline used
    # for auditable decisions: broad Candidate recall, one bounded rerank, and
    # a compact Evidence projection. STANDARD stays cheaper for ordinary work.
    "ENTERPRISE_EVIDENCE": ResolvedRetrievalProfile(
        "ENTERPRISE_EVIDENCE", "retrieval-profile/v1", 100, 10, True, 2, 100
    ),
    "DEEP_RESEARCH": ResolvedRetrievalProfile(
        "DEEP_RESEARCH", "retrieval-profile/v1", 100, 10, True, 3, 100
    ),
}


def resolve_retrieval_profile(
    requested: str,
    *,
    requested_revision: str = "",
    allowed_profiles: list[str] | None = None,
) -> ResolvedRetrievalProfile:
    """Resolve a known profile and reject revision or allow-list drift.

    Empty ``allowed_profiles`` is deliberately treated as backward-compatible
    local development. Published calls provide the list from their immutable
    snapshot and therefore fail closed for a profile outside that list.
    """

    name = (requested or "STANDARD").upper()
    profile = _PROFILES.get(name)
    if profile is None:
        raise ValueError(f"unknown retrieval profile: {name}")
    if requested_revision and requested_revision != profile.revision:
        raise ValueError("requested retrieval profile revision is not active")
    if allowed_profiles and name not in {str(item).upper() for item in allowed_profiles}:
        raise ValueError("requested retrieval profile is not allowed by the published snapshot")
    return profile
