from app.bootstrap.repository import build_repository
from app.core.config import get_settings
from app.rag.query_service import RagQueryService
from app.rerank import build_reranker
from app.retrieval.controlled_scan import ControlledFileScanner
from app.retrieval.embedder import build_embedder
from app.retrieval.hybrid_retriever import HybridRetriever
from app.retrieval.search_projection import build_search_projection
from app.retrieval.semantic_cache import SemanticResponseCache


class RagQueryContainer:
    """组装只读 RAG 查询面；索引更新和文档解析属于摄取服务。"""

    def __init__(self) -> None:
        """加载冻结配置并构建检索、精排、扫描和版本化索引依赖。"""
        self.settings = get_settings()
        self.repository = build_repository(self.settings)
        self.embedder = build_embedder(self.settings)
        self.reranker = build_reranker(self.settings)
        self.retriever = HybridRetriever(
            bm25_weight=self.settings.bm25_weight,
            vector_weight=self.settings.vector_weight,
            embedder=self.embedder,
            reranker=self.reranker,
            candidate_k=self.settings.retrieval_candidate_k,
        )
        self.search_projection = build_search_projection(self.settings, embedder=self.embedder)
        self.semantic_cache = SemanticResponseCache(
            backend=self.settings.semantic_cache_backend,
            redis_url=self.settings.redis_url,
            ttl_seconds=self.settings.semantic_cache_ttl_seconds,
            similarity_threshold=self.settings.semantic_cache_similarity_threshold,
            max_entries_per_partition=self.settings.semantic_cache_max_entries_per_partition,
        )
        self.scanner = ControlledFileScanner(
            self.settings.scan_roots,
            max_file_bytes=self.settings.scan_max_file_bytes,
            max_files=self.settings.scan_max_files,
            max_results=self.settings.scan_max_results,
        )
        self.query_service = RagQueryService(
            self.repository,
            self.retriever,
            allow_legacy_public_documents=self.settings.allow_legacy_public_documents,
            search_projection=self.search_projection,
            scanner=self.scanner,
            index_version=self.settings.opensearch_index_version,
            backend=self.settings.search_backend,
            embedding_contract=self.embedder.contract,
            reranker_revision=(
                f"{self.settings.rerank_provider}:{self.settings.rerank_model}:v1"
                if self.reranker is not None
                else "none/v1"
            ),
            semantic_cache=self.semantic_cache,
            query_embedder=self.embedder,
        )

    def close(self) -> None:
        """释放仓储连接；检索器和扫描器没有需关闭的持久资源。"""
        close = getattr(self.repository, "close", None)
        if close is not None:
            close()
