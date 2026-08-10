from pathlib import Path

from app.context.service import AgentContextService
from app.context.store import ConversationStore
from app.contracts.context import ContextAssembleRequest, ConversationMessage
from app.contracts.ingestion import IngestionJob, JobStatus
from app.contracts.rag import RagSearchRequest, RagSearchResponse
from app.domain.models import Evidence
from app.ingestion.job_store import IngestionJobStore
from app.rag.query_service import RagQueryService
from app.tools.registry import ToolRegistry

from agent_runtime_service.agent.graph import AgentGraph
from agent_runtime_service.agent.models import AgentAction, AgentDecision


class FakeRagClient:
    def search(self, request):
        return RagSearchResponse(
            query=request.query,
            evidence=[
                Evidence(
                    source_id="reg-1",
                    source_type="regulation",
                    text="Records must be attributable.",
                    score=0.9,
                )
            ],
            candidate_count=1,
        )


class FakeContextClient:
    def assemble(self, request):
        return AgentContextService(
            ConversationStore(), FakeRagClient(), max_messages=4, default_token_budget=1000
        ).assemble(request)


class SequenceDecisionEngine:
    def __init__(self):
        self.decisions = [
            AgentDecision(action=AgentAction.RETRIEVE, query="record controls"),
            AgentDecision(
                action=AgentAction.ANSWER,
                final_answer="Records must be attributable [reg-1].",
            ),
        ]

    def decide(self, state, tool_registry):
        return self.decisions.pop(0)


def test_context_service_combines_memory_and_rag() -> None:
    store = ConversationStore()
    service = AgentContextService(store, FakeRagClient(), max_messages=4, default_token_budget=1000)
    service.append_message(
        "session-1",
        ConversationMessage(role="user", content="Earlier question"),
    )

    package = service.assemble(
        ContextAssembleRequest(session_id="session-1", query="record controls")
    )

    assert package.recent_messages[0].content == "Earlier question"
    assert package.knowledge_evidence[0].source_id == "reg-1"


def test_agent_graph_uses_context_service_contract() -> None:
    graph = AgentGraph(
        SequenceDecisionEngine(),
        tool_registry=ToolRegistry(),
        context_client=FakeContextClient(),
    )

    result = graph.run(
        {
            "task": "record controls",
            "tenant_id": "tenant-a",
            "user_id": "user-a",
            "permissions": ["rag:read"],
            "request_id": "request-a",
            "session_id": "session-a",
            "step_count": 0,
            "max_steps": 4,
            "observations": [],
            "evidence": [],
        },
        "thread-a",
    )

    assert result.answer.endswith("[reg-1].")
    assert result.evidence[0]["source_id"] == "reg-1"


def test_runtime_reads_evidence_from_rag_contract_not_context_internals() -> None:
    class RecordingRagClient(FakeRagClient):
        def __init__(self) -> None:
            self.requests = []

        def search(self, request):
            self.requests.append(request)
            return super().search(request)

    rag_client = RecordingRagClient()
    graph = AgentGraph(
        SequenceDecisionEngine(),
        tool_registry=ToolRegistry(),
        context_client=FakeContextClient(),
        rag_client=rag_client,
    )

    result = graph.run(
        {
            "task": "record controls",
            "tenant_id": "tenant-a",
            "user_id": "user-a",
            "permissions": ["rag:read"],
            "request_id": "request-a",
            "session_id": "session-a",
            "step_count": 0,
            "max_steps": 4,
            "observations": [],
            "evidence": [],
        },
        "thread-a",
    )

    assert result.evidence[0]["source_id"] == "reg-1"
    assert [request.query for request in rag_client.requests] == ["record controls"]


def test_ingestion_job_claim_is_atomic(tmp_path: Path) -> None:
    store = IngestionJobStore(tmp_path / "jobs.db")
    created = store.create(IngestionJob(job_type="REINDEX"))

    claimed = store.claim_next()

    assert claimed is not None
    assert claimed.job_id == created.job_id
    assert claimed.status == JobStatus.RUNNING
    assert store.claim_next() is None
    store.complete(claimed, {"indexed": 3})
    assert store.get(created.job_id).status == JobStatus.COMPLETED
    store.close()


def test_context_memory_is_tenant_scoped() -> None:
    service = AgentContextService(
        ConversationStore(), FakeRagClient(), max_messages=4, default_token_budget=1000
    )
    service.append_message(
        "same-session",
        ConversationMessage(role="user", content="tenant A secret"),
        tenant_id="tenant-a",
        user_id="user-a",
    )

    package = service.assemble(
        ContextAssembleRequest(
            session_id="same-session",
            query="records",
            tenant_id="tenant-b",
            user_id="user-a",
        )
    )

    assert package.recent_messages == []


def test_rag_acl_is_applied_before_retrieval() -> None:
    class Repository:
        def regulation_chunks(self):
            from app.domain.models import Chunk

            return [
                Chunk(
                    source_id="private-a",
                    source_type="regulation",
                    text="private tenant evidence",
                    metadata={"tenant_id": "tenant-a"},
                )
            ]

        def get_document(self, document_id):
            return None

    class CapturingRetriever:
        def __init__(self):
            self.candidate_count = -1

        def search(self, query, chunks, top_k):
            self.candidate_count = len(chunks)
            return []

    retriever = CapturingRetriever()
    service = RagQueryService(Repository(), retriever)
    service.search(RagSearchRequest(query="private", tenant_id="tenant-b"))

    assert retriever.candidate_count == 0


def test_rag_contract_exposes_active_index_version() -> None:
    service = RagQueryService(
        repository=object(),
        retriever=object(),
        index_version="knowledge-2026-08-10",
        backend="opensearch",
    )

    assert service.index_version == "knowledge-2026-08-10"
    assert service.backend == "opensearch"
