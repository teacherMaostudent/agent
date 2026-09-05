"""RAG query orchestration with ACL-preserving retrieval and reranking."""

from opentelemetry import trace

from app.contracts.rag import RagSearchRequest, RagSearchResponse
from app.ingestion.chunker import TextChunker
from app.retrieval.conflict_detector import ConflictDetector
from app.retrieval.controlled_scan import ControlledFileScanner, ControlledScanUnavailableError
from app.retrieval.evidence_verifier import EvidenceVerifier
from app.retrieval.profiles import resolve_retrieval_profile
from app.retrieval.query_planner import QueryPlanner, fuse_query_candidates


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
        reranker_revision: str = "",
        semantic_cache=None,
        query_embedder=None,
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
        # Evidence verification is intentionally owned by the query plane,
        # after retrieval and before Context/LLM projection.
        self.evidence_verifier = EvidenceVerifier()
        self.conflict_detector = ConflictDetector()
        self.reranker_revision = reranker_revision
        self.query_planner = QueryPlanner()
        # The cache has no authority over ACL or retrieval. It is consulted
        # only after the server resolves the published profile and validates
        # all pinned contracts.
        self.semantic_cache = semantic_cache
        self.query_embedder = query_embedder

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
            profile = resolve_retrieval_profile(
                request.retrieval_profile,
                requested_revision=request.retrieval_profile_revision,
                allowed_profiles=request.metadata.get("allowed_retrieval_profiles"),
            )
            if profile.name == "NO_RAG":
                return RagSearchResponse(
                    query=request.query,
                    index_version=self.index_version,
                    embedding_contract_id=(
                        self.embedding_contract.contract_id if self.embedding_contract else ""
                    ),
                    retrieval_profile=profile.name,
                    retrieval_profile_revision=profile.revision,
                    reranker_revision=self.reranker_revision,
                )
            if request.index_version and request.index_version != self.index_version:
                raise ValueError("requested RAG index version is not active on this query service")
            if request.index_manifest_id:
                manifest = self.repository.get_index_manifest(request.index_manifest_id)
                if manifest is None or manifest.status != "READY":
                    raise ValueError("requested index build manifest is not READY")
                if (
                    manifest.tenant_id != request.tenant_id
                    or manifest.index_version != self.index_version
                ):
                    raise ValueError("requested index build manifest does not match tenant or active index")
            contract_id = (
                self.embedding_contract.contract_id if self.embedding_contract is not None else ""
            )
            if request.embedding_contract_id and request.embedding_contract_id != contract_id:
                raise ValueError("requested embedding contract does not match active RAG index")
            if request.reranker_contract_id and request.reranker_contract_id != self.reranker_revision:
                raise ValueError("requested reranker contract does not match active RAG query service")
            query_plan = self.query_planner.plan(
                request.query, max_variants=profile.max_query_variants
            )
            cached = self._cached_response(request, query_plan.normalized_query)
            if cached is not None:
                span.set_attribute("rag.cache_status", cached.cache_status)
                return cached
            span.set_attribute("rag.query_variant_count", len(query_plan.queries))
            if self.search_projection is not None and hasattr(self.search_projection, "search"):
                # Profile limits are enforced here rather than trusting the
                # legacy top_k field provided by any remote caller.
                controlled_request = request.model_copy(
                    update={"top_k": profile.candidate_top_k}
                )
                per_query = [
                    self.search_projection.search(
                        controlled_request.model_copy(update={"query": query})
                    ).candidates
                    for query in query_plan.queries
                ]
                candidates = fuse_query_candidates(per_query)[: profile.candidate_top_k]
                if profile.rerank_enabled and hasattr(self.retriever, "rerank"):
                    candidates = self.retriever.rerank(
                        request.query, candidates, profile.max_rerank_docs
                    )
                candidates = self.conflict_detector.mark(candidates)
                evidence = self.evidence_verifier.verify(
                    candidates,
                    tenant_id=request.tenant_id,
                    user_id=request.user_id,
                    index_version=self.index_version,
                    embedding_contract_id=contract_id,
                    retrieval_profile=profile.name,
                    retrieval_profile_revision=profile.revision,
                    reranker_revision=self.reranker_revision,
                    evidence_top_k=profile.evidence_top_k,
                    reject_conflicted=profile.name == "STRICT_EVIDENCE",
                )
                span.set_attribute("rag.candidate_count", len(candidates))
                span.set_attribute("rag.result_count", len(evidence))
                span.set_attribute("tenant.id", request.tenant_id)
                # The backing projection may not know the public alias/version;
                # normalize it at the service boundary for every backend.
                response = RagSearchResponse(
                    query=request.query,
                    evidence=evidence,
                    candidate_count=len(candidates),
                    index_version=self.index_version,
                    embedding_contract_id=contract_id,
                    retrieval_profile=profile.name,
                    retrieval_profile_revision=profile.revision,
                    reranker_revision=self.reranker_revision,
                    candidates=candidates,
                    cache_status="MISS" if self._cache_enabled else "BYPASS",
                )
                self._cache_response(request, query_plan.normalized_query, response)
                span.set_attribute("rag.cache_status", response.cache_status)
                return response
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
                        metadata={
                            **request.metadata,
                            "tenant_id": request.tenant_id,
                            "allowed_users": [request.user_id],
                            "temporary": True,
                        },
                    )
                )
            candidates = fuse_query_candidates(
                [
                    self.retriever.search(
                        query,
                        chunks,
                        profile.candidate_top_k,
                        rerank=False,
                    )
                    for query in query_plan.queries
                ]
            )[: profile.candidate_top_k]
            if profile.rerank_enabled and hasattr(self.retriever, "rerank"):
                candidates = self.retriever.rerank(
                    request.query, candidates, profile.max_rerank_docs
                )
            candidates = self.conflict_detector.mark(candidates)
            evidence = self.evidence_verifier.verify(
                candidates,
                tenant_id=request.tenant_id,
                user_id=request.user_id,
                index_version=self.index_version,
                embedding_contract_id=contract_id,
                retrieval_profile=profile.name,
                retrieval_profile_revision=profile.revision,
                reranker_revision=self.reranker_revision,
                evidence_top_k=profile.evidence_top_k,
                reject_conflicted=profile.name == "STRICT_EVIDENCE",
            )
            span.set_attribute("rag.candidate_count", len(chunks))
            span.set_attribute("rag.result_count", len(evidence))
            span.set_attribute("tenant.id", request.tenant_id)
            response = RagSearchResponse(
                query=request.query,
                evidence=evidence,
                candidate_count=len(chunks),
                index_version=self.index_version,
                embedding_contract_id=contract_id,
                retrieval_profile=profile.name,
                retrieval_profile_revision=profile.revision,
                reranker_revision=self.reranker_revision,
                candidates=candidates,
                cache_status="MISS" if self._cache_enabled else "BYPASS",
            )
            self._cache_response(request, query_plan.normalized_query, response)
            span.set_attribute("rag.cache_status", response.cache_status)
            return response

    def _cached_response(self, request: RagSearchRequest, normalized_query: str):
        """Ask the optional cache for a response only after release contracts are checked."""
        if self.semantic_cache is None or self.query_embedder is None:
            return None
        return self.semantic_cache.get(
            request, normalized_query=normalized_query, embedder=self.query_embedder
        )

    @property
    def _cache_enabled(self) -> bool:
        """Keep disabled cache objects observable as BYPASS rather than a misleading MISS."""
        return bool(self.semantic_cache is not None and self.semantic_cache.enabled)

    def _cache_response(
        self, request: RagSearchRequest, normalized_query: str, response: RagSearchResponse
    ) -> None:
        """Write verified results opportunistically; cache failures cannot fail retrieval."""
        if self.semantic_cache is None or self.query_embedder is None:
            return
        self.semantic_cache.put(
            request,
            normalized_query=normalized_query,
            response=response,
            embedder=self.query_embedder,
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
