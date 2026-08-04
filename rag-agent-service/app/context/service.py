"""Context assembly with explicit evidence/memory degradation semantics.

Conversation history is tenant-scoped and ranked independently from retrieved
evidence.  When optional RAG is unavailable the service returns a labelled
memory-only package, never an unmarked empty evidence result.
"""

from __future__ import annotations

import logging
import math
import re
from datetime import UTC, datetime

from opentelemetry import trace

from app.contracts.context import (
    ContextAssembleRequest,
    ContextBudgetReport,
    ContextPackage,
    ConversationMessage,
)
from app.contracts.rag import RagSearchRequest
from app.domain.models import Evidence

log = logging.getLogger(__name__)
_TOKEN = re.compile(r"[\w\u4e00-\u9fff]+", re.UNICODE)
_ROLE_WEIGHT = {"system": 1.0, "tool": 0.9, "user": 0.82, "assistant": 0.7}
_SOURCE_TRUST = {
    "regulation": 0.98,
    "policy": 0.95,
    "enterprise_document": 0.85,
    "knowledge_base": 0.82,
    "tool": 0.72,
}


class AgentContextService:
    """Compose ranked, bounded memory and optional RAG evidence."""

    def __init__(
        self,
        store,
        rag_client,
        max_messages: int,
        default_token_budget: int,
        message_budget_ratio: float = 0.4,
    ) -> None:
        self.store = store
        self.rag_client = rag_client
        self.max_messages = max_messages
        self.default_token_budget = default_token_budget
        self.message_budget_ratio = message_budget_ratio

    def append_message(
        self,
        session_id: str,
        message: ConversationMessage,
        tenant_id: str = "default",
        user_id: str = "anonymous",
    ) -> None:
        # Tenant and user are part of the storage key; session ids alone are
        # not safe isolation boundaries in a multi-tenant runtime.
        self.store.append(self._session_key(tenant_id, user_id, session_id), message)

    def messages(
        self,
        session_id: str,
        tenant_id: str = "default",
        user_id: str = "anonymous",
    ) -> list[ConversationMessage]:
        return self.store.list_messages(self._session_key(tenant_id, user_id, session_id))

    def delete_session(
        self,
        session_id: str,
        tenant_id: str = "default",
        user_id: str = "anonymous",
    ) -> bool:
        return self.store.delete(self._session_key(tenant_id, user_id, session_id))

    def assemble(self, request: ContextAssembleRequest) -> ContextPackage:
        """Produce a deterministic, token-bounded prompt context package."""
        budget = request.token_budget or self.default_token_budget
        with trace.get_tracer(__name__).start_as_current_span("context.assemble") as span:
            messages = self.messages(
                request.session_id, request.tenant_id, request.user_id
            )[-self.max_messages :]
            evidence: list[Evidence] = []
            rag_status = "not_requested"
            degraded = False
            degrade_reason = None
            if request.include_rag:
                try:
                    rag_result = self.rag_client.search(
                        RagSearchRequest(
                            query=request.query,
                            tenant_id=request.tenant_id,
                            user_id=request.user_id,
                            document_id=request.document_id,
                            content=request.content,
                            metadata=request.metadata,
                            top_k=request.top_k,
                        )
                    )
                    evidence = rag_result.evidence
                    rag_status = "available"
                except Exception as exc:  # adapters expose different transport errors
                    if request.rag_required:
                        raise
                    # RAG is an optional dependency for conversational work.
                    # Returning ranked session memory keeps the Runtime usable
                    # without silently pretending that evidence was retrieved.
                    degraded = True
                    rag_status = "degraded"
                    degrade_reason = f"rag_unavailable:{type(exc).__name__}"
                    log.warning(
                        "context_rag_memory_only_fallback",
                        extra={
                            "tenant_id": request.tenant_id,
                            "session_id": request.session_id,
                            "error_type": type(exc).__name__,
                        },
                    )
            messages, evidence, report = self._rank_and_fit(
                messages, evidence, request.query, budget
            )
            truncated = bool(report.dropped_messages or report.dropped_evidence)
            span.set_attribute(
                "context.estimated_tokens",
                report.used_message_tokens + report.used_evidence_tokens,
            )
            span.set_attribute("context.truncated", truncated)
            span.set_attribute("context.rag_status", rag_status)
            return ContextPackage(
                session_id=request.session_id,
                recent_messages=messages,
                knowledge_evidence=evidence,
                user_context={
                    "tenant_id": request.tenant_id,
                    "user_id": request.user_id,
                },
                token_budget=budget,
                estimated_tokens=(
                    report.used_message_tokens + report.used_evidence_tokens
                ),
                truncated=truncated,
                rag_status=rag_status,
                degraded=degraded,
                degrade_reason=degrade_reason,
                budget_report=report,
            )

    @staticmethod
    def _session_key(tenant_id: str, user_id: str, session_id: str) -> str:
        return f"{tenant_id}:{user_id}:{session_id}"

    def _rank_and_fit(
        self,
        messages: list[ConversationMessage],
        evidence: list[Evidence],
        query: str,
        budget: int,
    ) -> tuple[list[ConversationMessage], list[Evidence], ContextBudgetReport]:
        """Allocate token budget by ranked value while retaining both context types."""
        ranked_messages = self._rank_messages(messages, query)
        ranked_evidence = self._rank_evidence(evidence, query)
        if not ranked_evidence:
            message_budget, evidence_budget = budget, 0
        elif not ranked_messages:
            message_budget, evidence_budget = 0, budget
        else:
            # Reserve capacity for both conversational continuity and grounded
            # evidence.  Leftover tokens are then awarded by the same ranking
            # score, making budget decisions explainable and deterministic.
            message_budget = int(budget * self.message_budget_ratio)
            evidence_budget = budget - message_budget

        selected_messages, rejected_messages, used_messages = self._select(
            ranked_messages, message_budget, lambda item: item.content
        )
        selected_evidence, rejected_evidence, used_evidence = self._select(
            ranked_evidence, evidence_budget, lambda item: item.text
        )
        spare = budget - used_messages - used_evidence
        candidates = [
            (float(item.metadata["context_ranking"]["score"]), "message", item)
            for item in rejected_messages
        ] + [
            (float(item.metadata["context_ranking"]["score"]), "evidence", item)
            for item in rejected_evidence
        ]
        for _, kind, item in sorted(candidates, key=lambda row: row[0], reverse=True):
            tokens = self._estimate(item.content if kind == "message" else item.text)
            if tokens > spare:
                continue
            spare -= tokens
            if kind == "message":
                selected_messages.append(item)
                used_messages += tokens
                rejected_messages.remove(item)
            else:
                selected_evidence.append(item)
                used_evidence += tokens
                rejected_evidence.remove(item)

        selected_messages.sort(key=lambda item: item.created_at)
        selected_evidence.sort(
            key=lambda item: float(item.metadata["context_ranking"]["score"]),
            reverse=True,
        )
        report = ContextBudgetReport(
            requested_tokens=budget,
            message_budget=message_budget,
            evidence_budget=evidence_budget,
            used_message_tokens=used_messages,
            used_evidence_tokens=used_evidence,
            dropped_messages=len(rejected_messages),
            dropped_evidence=len(rejected_evidence),
        )
        return selected_messages, selected_evidence, report

    @staticmethod
    def _select(items, limit: int, text):
        selected, rejected, used = [], [], 0
        for item in items:
            tokens = AgentContextService._estimate(text(item))
            if used + tokens <= limit:
                selected.append(item)
                used += tokens
            else:
                rejected.append(item)
        return selected, rejected, used

    @staticmethod
    def _rank_messages(
        messages: list[ConversationMessage], query: str
    ) -> list[ConversationMessage]:
        now = datetime.now(UTC)
        ranked = []
        for item in messages:
            age_days = max(0.0, (now - item.created_at).total_seconds() / 86_400)
            role = _ROLE_WEIGHT.get(item.role, 0.5)
            recency = math.exp(-age_days / 30)
            relevance = AgentContextService._relevance(query, item.content)
            score = 0.4 * role + 0.35 * recency + 0.25 * relevance
            ranked.append(
                item.model_copy(
                    update={
                        "metadata": {
                            **item.metadata,
                            "context_ranking": {
                                "score": round(score, 6),
                                "role": round(role, 6),
                                "recency": round(recency, 6),
                                "relevance": round(relevance, 6),
                            },
                        }
                    }
                )
            )
        return sorted(
            ranked,
            key=lambda item: float(item.metadata["context_ranking"]["score"]),
            reverse=True,
        )

    @staticmethod
    def _rank_evidence(evidence: list[Evidence], query: str) -> list[Evidence]:
        now = datetime.now(UTC)
        max_score = max((max(0.0, item.score) for item in evidence), default=1.0)
        ranked = []
        for item in evidence:
            retrieval = max(0.0, item.score) / max(max_score, 1e-9)
            semantic = AgentContextService._relevance(query, item.text)
            relevance = 0.8 * retrieval + 0.2 * semantic
            trust = AgentContextService._bounded_score(
                item.metadata.get("trust_score"),
                default=_SOURCE_TRUST.get(item.source_type, 0.6),
            )
            timestamp = AgentContextService._timestamp(item.metadata)
            age_days = max(0.0, (now - timestamp).total_seconds() / 86_400)
            recency = math.exp(-age_days / 180)
            score = 0.6 * relevance + 0.3 * trust + 0.1 * recency
            ranked.append(
                item.model_copy(
                    update={
                        "metadata": {
                            **item.metadata,
                            "context_ranking": {
                                "score": round(score, 6),
                                "relevance": round(relevance, 6),
                                "source_trust": round(trust, 6),
                                "recency": round(recency, 6),
                            },
                        }
                    }
                )
            )
        return sorted(
            ranked,
            key=lambda item: float(item.metadata["context_ranking"]["score"]),
            reverse=True,
        )

    @staticmethod
    def _bounded_score(value: object, *, default: float) -> float:
        """Parse an external score without letting bad metadata break context assembly."""
        try:
            score = float(value) if value is not None else default
        except (TypeError, ValueError):
            score = default
        return min(1.0, max(0.0, score))

    @staticmethod
    def _timestamp(metadata: dict) -> datetime:
        for name in ("updated_at", "published_at", "created_at"):
            value = metadata.get(name)
            if not value:
                continue
            try:
                parsed = datetime.fromisoformat(str(value))
                return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
            except ValueError:
                continue
        return datetime.now(UTC)

    @staticmethod
    def _relevance(query: str, text: str) -> float:
        query_tokens = {token.lower() for token in _TOKEN.findall(query)}
        if not query_tokens:
            return 0.0
        text_tokens = {token.lower() for token in _TOKEN.findall(text)}
        return len(query_tokens & text_tokens) / len(query_tokens)

    @staticmethod
    def _estimate(text: str) -> int:
        return max(1, len(text) // 4)
