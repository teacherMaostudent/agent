"""Candidate-to-Evidence trust boundary for the online RAG query plane."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from datetime import UTC, datetime

from app.domain.models import Evidence, RetrievalCandidate


class EvidenceVerifier:
    """Revalidate candidates before their text can be projected into Context.

    Retriever ACL filters are the primary protection.  This verifier is a
    deliberate second line of defence for local backends, stale indexes, and
    future providers whose filter semantics differ from OpenSearch.
    """

    def verify(
        self,
        candidates: Iterable[RetrievalCandidate],
        *,
        tenant_id: str,
        user_id: str,
        index_version: str,
        embedding_contract_id: str,
        retrieval_profile: str,
        retrieval_profile_revision: str,
        reranker_revision: str,
        evidence_top_k: int,
        reject_conflicted: bool = False,
    ) -> list[Evidence]:
        """Return only authorized, current and integrity-preserving evidence."""

        verified: list[Evidence] = []
        for candidate in candidates:
            if len(verified) >= evidence_top_k:
                break
            metadata = candidate.metadata
            if not self._authorized(metadata, tenant_id, user_id):
                continue
            if not self._current(metadata):
                continue
            if not self._source_is_eligible(metadata):
                continue
            if reject_conflicted and metadata.get("conflict"):
                continue
            # A source can opt into digest enforcement when the ingestion
            # manifest provides content_sha256.  Missing legacy hashes do not
            # silently manufacture a successful integrity assertion.
            expected_hash = str(metadata.get("content_sha256", ""))
            actual_hash = hashlib.sha256(candidate.text.encode("utf-8")).hexdigest()
            if expected_hash and expected_hash != actual_hash:
                continue
            if self._looks_like_prompt_instruction(candidate.text, metadata):
                continue
            verified.append(
                Evidence(
                    source_id=candidate.source_id,
                    source_type=candidate.source_type,
                    text=candidate.text,
                    score=candidate.score,
                    chunk_id=candidate.chunk_id,
                    document_id=candidate.document_id or str(metadata.get("document_id", "")),
                    document_version=candidate.document_version
                    or str(metadata.get("document_version", "")),
                    index_version=index_version,
                    embedding_contract_id=embedding_contract_id,
                    retrieval_profile=retrieval_profile,
                    retrieval_profile_revision=retrieval_profile_revision,
                    reranker_revision=reranker_revision,
                    retrieval_channels=self._channels(candidate),
                    metadata={**metadata, "candidate_id": candidate.candidate_id},
                )
            )
        return verified

    @staticmethod
    def _authorized(metadata: dict, tenant_id: str, user_id: str) -> bool:
        """Fail closed for unowned sources; user allow lists remain tenant-scoped."""

        if metadata.get("tenant_id") != tenant_id:
            return False
        allowed_users = metadata.get("allowed_users") or []
        return not allowed_users or user_id in allowed_users

    @staticmethod
    def _current(metadata: dict) -> bool:
        """Reject superseded/non-active material and expired evidence at projection time."""

        if metadata.get("knowledge_status", "active") != "active":
            return False
        valid_until = metadata.get("valid_until")
        if not valid_until:
            return True
        try:
            value = datetime.fromisoformat(str(valid_until).replace("Z", "+00:00"))
            return value.astimezone(UTC) > datetime.now(UTC)
        except ValueError:
            return False

    @staticmethod
    def _source_is_eligible(metadata: dict) -> bool:
        """Reject explicitly revoked/quarantined source records before Context sees them.

        Source authority is normally established during ingestion and recorded
        as ``source_status``.  Missing status is retained only for legacy
        records; new ingestion writes ``active`` explicitly so a later source
        revocation works without rebuilding every consumer's authorization.
        """

        return str(metadata.get("source_status", "active")).lower() not in {
            "revoked",
            "quarantined",
            "untrusted",
        }

    @staticmethod
    def _channels(candidate: RetrievalCandidate) -> list:
        """Preserve all fused retrieval channels instead of flattening them to HYBRID."""

        values = candidate.metadata.get("retrieval_channels") or [candidate.channel]
        result = []
        for item in values:
            if isinstance(item, type(candidate.channel)):
                result.append(item)
                continue
            try:
                # Provider metadata is diagnostic rather than trusted input;
                # accept case-only serialization differences but never permit
                # an invalid value to create a synthetic channel.
                result.append(type(candidate.channel)(str(item).upper()))
            except ValueError:
                continue
        return result or [candidate.channel]

    @staticmethod
    def _looks_like_prompt_instruction(text: str, metadata: dict) -> bool:
        """Block known instruction-like untrusted evidence unless explicitly classified safe.

        This is a defence-in-depth heuristic, not a claim that prompt injection
        is fully solved. The original item remains available for human review in
        the source store and is never silently rewritten here.
        """

        if metadata.get("instruction_allowed") is True:
            return False
        lowered = text.lower()
        markers = ("ignore previous instructions", "system message", "忽略此前指令", "忽略以上指令")
        return any(marker in lowered for marker in markers)
