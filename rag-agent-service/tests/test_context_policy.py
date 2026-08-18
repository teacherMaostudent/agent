from datetime import UTC, datetime, timedelta

import pytest

from app.context.service import AgentContextService
from app.context.store import ConversationStore
from app.contracts.context import ContextAssembleRequest, ConversationMessage
from app.contracts.rag import RagSearchResponse
from app.domain.models import Evidence


class FailingRag:
    def search(self, request):
        raise RuntimeError("rag unavailable")


class RankedRag:
    def search(self, request):
        return RagSearchResponse(
            query=request.query,
            evidence=[
                Evidence(
                    source_id="untrusted",
                    source_type="unknown",
                    text="audit requirement",
                    score=1.0,
                    metadata={"trust_score": "invalid external score"},
                ),
                Evidence(
                    source_id="regulation",
                    source_type="regulation",
                    text="audit requirement",
                    score=0.8,
                    metadata={"trust_score": 1.0},
                ),
            ],
            candidate_count=2,
        )


def test_optional_rag_failure_returns_ranked_memory_only_context() -> None:
    store = ConversationStore()
    service = AgentContextService(store, FailingRag(), 10, 1_000)
    service.append_message(
        "session-a",
        ConversationMessage(role="system", content="Keep the audit scope narrow."),
        "tenant-a",
        "user-a",
    )

    package = service.assemble(
        ContextAssembleRequest(
            session_id="session-a",
            query="audit scope",
            tenant_id="tenant-a",
            user_id="user-a",
            rag_required=False,
        )
    )

    assert package.degraded is True
    assert package.rag_status == "degraded"
    assert package.degrade_reason == "rag_unavailable:RuntimeError"
    assert package.knowledge_evidence == []
    assert package.recent_messages[0].metadata["context_ranking"]["role"] == 1.0
    assert package.budget_report.used_message_tokens > 0
    assert service.pipeline.stage_names == ("history", "rag", "token_budget")


def test_required_rag_failure_is_not_silently_degraded() -> None:
    service = AgentContextService(ConversationStore(), FailingRag(), 10, 1_000)

    with pytest.raises(RuntimeError, match="rag unavailable"):
        service.assemble(ContextAssembleRequest(session_id="session-a", query="audit scope"))


def test_context_combines_role_time_relevance_and_source_trust() -> None:
    store = ConversationStore()
    service = AgentContextService(store, RankedRag(), 10, 512)
    old = datetime.now(UTC) - timedelta(days=20)
    service.append_message(
        "session-a",
        ConversationMessage(role="assistant", content="unrelated answer", created_at=old),
        "tenant-a",
        "user-a",
    )
    service.append_message(
        "session-a",
        ConversationMessage(role="system", content="audit requirement policy"),
        "tenant-a",
        "user-a",
    )

    package = service.assemble(
        ContextAssembleRequest(
            session_id="session-a",
            query="audit requirement",
            tenant_id="tenant-a",
            user_id="user-a",
        )
    )

    system = next(item for item in package.recent_messages if item.role == "system")
    assistant = next(item for item in package.recent_messages if item.role == "assistant")
    assert (
        system.metadata["context_ranking"]["score"] > assistant.metadata["context_ranking"]["score"]
    )
    assert package.knowledge_evidence[0].source_id == "regulation"
    untrusted = next(item for item in package.knowledge_evidence if item.source_id == "untrusted")
    assert untrusted.metadata["context_ranking"]["source_trust"] == 0.6
    assert package.budget_report.message_budget == 204
    assert package.budget_report.evidence_budget == 308
    assert package.estimated_tokens == (
        package.budget_report.used_message_tokens + package.budget_report.used_evidence_tokens
    )


def test_context_message_idempotency_key_prevents_mailbox_retry_duplication() -> None:
    """Runtime 邮箱重试同一输入时，Context 只保留一份原始消息正文。"""
    store = ConversationStore()
    message = ConversationMessage(
        role="user",
        content="Please steer the current run toward policy evidence.",
        metadata={"idempotency_key": "mailbox-request-1"},
    )
    store.append("tenant-a:user-a:session-a", message)
    store.append("tenant-a:user-a:session-a", message)

    assert len(store.list_messages("tenant-a:user-a:session-a")) == 1
