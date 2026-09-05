"""Compatibility import path; shared models now belong to platform-sdk."""

from platform_sdk.contracts.models import (
    Chunk,
    Document,
    Evidence,
    RetrievalCandidate,
    RetrievalChannel,
    new_id,
    utc_now,
)

__all__ = [
    "Chunk",
    "Document",
    "Evidence",
    "RetrievalCandidate",
    "RetrievalChannel",
    "new_id",
    "utc_now",
]
