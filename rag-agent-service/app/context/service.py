from opentelemetry import trace

from app.contracts.context import ContextAssembleRequest, ContextPackage, ConversationMessage
from app.contracts.rag import RagSearchRequest


class AgentContextService:
    """Owns conversation memory and composes bounded context from RAG evidence."""

    def __init__(self, store, rag_client, max_messages: int, default_token_budget: int) -> None:
        self.store = store
        self.rag_client = rag_client
        self.max_messages = max_messages
        self.default_token_budget = default_token_budget

    def append_message(
        self,
        session_id: str,
        message: ConversationMessage,
        tenant_id: str = "default",
        user_id: str = "anonymous",
    ) -> None:
        self.store.append(self._session_key(tenant_id, user_id, session_id), message)

    def messages(
        self,
        session_id: str,
        tenant_id: str = "default",
        user_id: str = "anonymous",
    ) -> list[ConversationMessage]:
        return self.store.list_messages(self._session_key(tenant_id, user_id, session_id))

    def assemble(self, request: ContextAssembleRequest) -> ContextPackage:
        budget = request.token_budget or self.default_token_budget
        with trace.get_tracer(__name__).start_as_current_span("context.assemble") as span:
            messages = self.messages(
                request.session_id, request.tenant_id, request.user_id
            )[-self.max_messages :]
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
            messages, evidence, estimated, truncated = self._fit_budget(
                messages, rag_result.evidence, budget
            )
            span.set_attribute("context.estimated_tokens", estimated)
            span.set_attribute("context.truncated", truncated)
            return ContextPackage(
                session_id=request.session_id,
                recent_messages=messages,
                knowledge_evidence=evidence,
                user_context={
                    "tenant_id": request.tenant_id,
                    "user_id": request.user_id,
                },
                token_budget=budget,
                estimated_tokens=estimated,
                truncated=truncated,
            )

    @staticmethod
    def _session_key(tenant_id: str, user_id: str, session_id: str) -> str:
        return f"{tenant_id}:{user_id}:{session_id}"

    @staticmethod
    def _fit_budget(messages, evidence, budget: int):
        # A deterministic approximation is preferable to a model-specific tokenizer here.
        estimate = lambda text: max(1, len(text) // 4)
        selected_messages = list(messages)
        selected_evidence = list(evidence)
        total = sum(estimate(item.content) for item in selected_messages)
        total += sum(estimate(item.text) for item in selected_evidence)
        truncated = False
        while total > budget and selected_messages:
            removed = selected_messages.pop(0)
            total -= estimate(removed.content)
            truncated = True
        while total > budget and selected_evidence:
            removed = selected_evidence.pop()
            total -= estimate(removed.text)
            truncated = True
        return selected_messages, selected_evidence, max(total, 0), truncated
