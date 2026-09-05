"""Deterministic conflict marking for evidence candidates.

Semantic contradiction judgment belongs to a separately evaluated model or
human review. This detector only acts on explicit, ingestion-supplied claim
keys/values, which makes its behaviour reproducible and safe for hard gates.
"""

from __future__ import annotations

from app.domain.models import RetrievalCandidate


class ConflictDetector:
    """Mark candidates that state incompatible normalized values for one claim key."""

    def mark(self, candidates: list[RetrievalCandidate]) -> list[RetrievalCandidate]:
        """Return copies carrying conflict metadata; source text is never changed."""

        values: dict[str, set[str]] = {}
        for candidate in candidates:
            key = str(candidate.metadata.get("claim_key", "")).strip()
            value = str(candidate.metadata.get("claim_value", "")).strip()
            if key and value:
                values.setdefault(key, set()).add(value)
        conflicted = {key for key, claim_values in values.items() if len(claim_values) > 1}
        return [
            candidate.model_copy(
                update={
                    "metadata": {
                        **candidate.metadata,
                        "conflict": bool(
                            str(candidate.metadata.get("claim_key", "")) in conflicted
                        ),
                        "conflict_detector_revision": "metadata-claims/v1",
                    }
                }
            )
            for candidate in candidates
        ]
