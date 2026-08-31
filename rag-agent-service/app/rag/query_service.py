"""RAG query orchestration with ACL-preserving retrieval and reranking."""

from opentelemetry import trace

from app.contracts.rag import RagSearchRequest, RagSearchResponse
from app.ingestion.chunker import TextChunker
from app.retrieval.controlled_scan import ControlledFileScanner, ControlledScanUnavailableError


class RagQueryService:
    """Return authorized evidence without deciding the Agent's next action.

    This online query plane never parses files or mutates the knowledge base;
    ingestion and workflow decisions stay in their own services.
    """

    def __init__(
        self,
        repository,
        retriever,
        *,
        allow_legacy_public_documents: bool = False,
        search_projection=None,
        scanner: ControlledFileScanner | None = None,
        index_version: str = "local",
        backend: str = "local",
        embedding_contract=None,
    ) -> None:
        """装配只读检索所需的仓储、索引、扫描、精排与版本选择依赖。"""
        self.repository = repository
        self.retriever = retriever
        self.chunker = TextChunker()
        self.allow_legacy_public_documents = allow_legacy_public_documents
        self.search_projection = search_projection
        self.scanner = scanner
        # Runtime pins this externally visible identity in a release snapshot;
        # it never needs to know whether the implementation is OpenSearch or local.
        self.index_version = index_version
        self.backend = backend
        self.embedding_contract = embedding_contract

    def scan(self, scope: str, pattern: str, *, regex: bool = False, glob: str = "") -> list[dict]:
        """执行受控文本扫描；scope 必须是服务配置的目录别名而不是客户端路径。"""
        if self.scanner is None:
            raise ControlledScanUnavailableError("controlled file scanning is not configured")
        return [
            item.__dict__
            for item in self.scanner.scan(scope, pattern, regex=regex, glob=glob or "**/*")
        ]

    def search(self, request: RagSearchRequest) -> RagSearchResponse:
        """返回经 ACL 约束的证据，不解析上传文件、不写知识库，也不规划 Agent 行为。"""
        with trace.get_tracer(__name__).start_as_current_span("rag.query.search") as span:
            if request.index_version and request.index_version != self.index_version:
                raise ValueError("requested RAG index version is not active on this query service")
            contract_id = (
                self.embedding_contract.contract_id if self.embedding_contract is not None else ""
            )
            if request.embedding_contract_id and request.embedding_contract_id != contract_id:
                raise ValueError("requested embedding contract does not match active RAG index")
            if self.search_projection is not None and hasattr(self.search_projection, "search"):
                result = self.search_projection.search(request)
                span.set_attribute("rag.result_count", len(result.evidence))
                span.set_attribute("tenant.id", request.tenant_id)
                # The backing projection may not know the public alias/version;
                # normalize it at the service boundary for every backend.
                return result.model_copy(
                    update={
                        "index_version": self.index_version,
                        "embedding_contract_id": contract_id,
                    }
                )
            # A general platform starts from tenant-owned documents; no domain
            # regulation seed corpus is implicitly injected into retrieval.
            chunks = []
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
                index_version=self.index_version,
                embedding_contract_id=contract_id,
            )

    def _authorized(self, metadata: dict, tenant_id: str, user_id: str) -> bool:
        """在检索前执行租户与用户 ACL，并默认拒绝无归属的历史数据。"""
        owner_tenant = metadata.get("tenant_id")
        if not owner_tenant:
            return self.allow_legacy_public_documents
        if owner_tenant and owner_tenant != tenant_id:
            return False
        allowed_users = metadata.get("allowed_users")
        return not allowed_users or user_id in allowed_users
