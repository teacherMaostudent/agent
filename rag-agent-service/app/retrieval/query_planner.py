"""Bounded deterministic query planning for Agentic RAG.

This component intentionally plans retrieval expressions only. It does not
decide Agent actions, invoke tools, or call an LLM; Runtime remains owner of
the overall plan/execute loop.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.domain.models import RetrievalCandidate

_SPACE = re.compile(r"\s+")
_IDENTIFIER = re.compile(r"\b[A-Za-z][A-Za-z0-9_]{1,30}(?:[-_/][A-Za-z0-9_]{1,30})+\b")


@dataclass(frozen=True)
class QueryPlan:
    """A small, auditable set of safe retrieval expressions for one user query."""

    original_query: str
    normalized_query: str
    queries: list[str]
    reason_codes: list[str]


class QueryPlanner:
    """Create deterministic variants under a profile-owned variant budget."""

    def plan(self, query: str, *, max_variants: int) -> QueryPlan:
        """Normalize whitespace and preserve exact identifiers as separate queries.

        Arbitrary model-generated expansions are deliberately absent here: they
        require a separately versioned rewrite model and evaluation release.
        """

        normalized = _SPACE.sub(" ", query).strip()
        queries = [normalized]
        reasons = ["QUERY_NORMALIZED"]
        for identifier in _IDENTIFIER.findall(normalized):
            if identifier not in queries and len(queries) < max_variants:
                queries.append(identifier)
                reasons.append("EXACT_IDENTIFIER_RECALL")
        return QueryPlan(query, normalized, queries[:max_variants], reasons)


def fuse_query_candidates(
    per_query: list[list[RetrievalCandidate]], *, k: int = 60
) -> list[RetrievalCandidate]:
    """RRF-fuse bounded query variants and retain the query-plan contribution.

    This is separate from channel fusion: each input list has already been
    fused across dense/lexical channels. Query variants are fused only by rank
    so one expansion cannot dominate due to incomparable backend scores.
    """

    merged: dict[str, RetrievalCandidate] = {}
    variants: dict[str, list[int]] = {}
    for variant_index, candidates in enumerate(per_query):
        for rank, candidate in enumerate(candidates, start=1):
            key = candidate.chunk_id or f"{candidate.source_id}:{candidate.metadata.get('start', 0)}"
            contribution = 1.0 / (k + rank)
            if key not in merged:
                merged[key] = candidate.model_copy(update={"score": contribution})
                variants[key] = [variant_index]
            else:
                merged[key].score += contribution
                variants[key].append(variant_index)
    ordered = sorted(merged.items(), key=lambda item: (-item[1].score, item[0]))
    return [
        candidate.model_copy(
            update={
                "rank": rank,
                "metadata": {
                    **candidate.metadata,
                    "query_fusion": "RRF",
                    "query_fusion_revision": "query-rrf/v1",
                    "query_variant_indexes": variants[key],
                },
            }
        )
        for rank, (key, candidate) in enumerate(ordered, start=1)
    ]
