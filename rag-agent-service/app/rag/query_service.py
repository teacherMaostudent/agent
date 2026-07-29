from opentelemetry import trace

from app.contracts.rag import RagSearchRequest, RagSearchResponse
from app.ingestion.chunker import TextChunker


class RagQueryService:
    """Online retrieval plane. It never parses files or mutates the knowledge base."""

    def __init__(self, repository, retriever) -> None:
        self.repository = repository
        self.retriever = retriever
        self.chunker = TextChunker()

    def search(self, request: RagSearchRequest) -> RagSearchResponse:
        with trace.get_tracer(__name__).start_as_current_span("rag.query.search") as span:
            chunks = [
                chunk
                for chunk in self.repository.regulation_chunks()
                if self._authorized(chunk.metadata, request.tenant_id, request.user_id)
            ]
            if request.document_id:
                document = self.repository.get_document(request.document_id)
                if document is not None and document.text:
                    chunks.extend(
                        chunk
                        for chunk in self.repository.document_chunks(request.document_id)
                        if self._authorized(chunk.metadata, request.tenant_id, request.user_id)
                    )
            if request.content:
                chunks.extend(
                    self.chunker.chunk(
                        source_id=f"inline:{request.tenant_id}:{request.user_id}",
                        source_type="enterprise_document",
                        text=request.content,
                        metadata={**request.metadata, "temporary": True},
                    )
                )
            evidence = self.retriever.search(request.query, chunks, request.top_k)
            span.set_attribute("rag.candidate_count", len(chunks))
            span.set_attribute("rag.result_count", len(evidence))
            span.set_attribute("tenant.id", request.tenant_id)
            return RagSearchResponse(
                query=request.query,
                evidence=evidence,
                candidate_count=len(chunks),
            )

    @staticmethod
    def _authorized(metadata: dict, tenant_id: str, user_id: str) -> bool:
        """Apply ACL before retrieval; missing ACL metadata means legacy/public data."""
        owner_tenant = metadata.get("tenant_id")
        if owner_tenant and owner_tenant != tenant_id:
            return False
        allowed_users = metadata.get("allowed_users")
        if allowed_users and user_id not in allowed_users:
            return False
        return True
