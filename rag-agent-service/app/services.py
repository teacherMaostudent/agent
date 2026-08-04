import logging

from app.core.config import get_settings
from app.bootstrap.repository import build_repository
from app.agent.decision_engine import GatewayDecisionEngine, OfflineDecisionEngine
from app.agent.graph import AgentGraph
from app.runtime.harness import AgentHarness
from app.generation.document_generator import DocumentGenerator
from app.generation.llm_client import LlmChatClient
from app.ingestion.parsers import DocumentParser
from app.infrastructure.llm_gateway_client import LlmGatewayClient
from app.knowledge.regulation_indexer import RegulationIndexer
from app.report.markdown_renderer import MarkdownReportRenderer
from app.retrieval.embedder import build_embedder
from app.retrieval.embedding_store import EmbeddingStore
from app.retrieval.hybrid_retriever import HybridRetriever
from app.rerank import build_reranker
from app.retrieval.semantic_retriever import SemanticRetriever
from app.review.cross_document_reviewer import CrossDocumentReviewer
from app.review.gmp_reviewer import GmpReviewService
from app.review.llm_judge import LlmJudge
from app.storage.factory import build_file_storage
from app.storage.snapshot_store import SnapshotStore
from app.tools.business import build_business_tool_registry

log = logging.getLogger(__name__)


class AppContainer:
    def __init__(self) -> None:
        self.settings = get_settings()
        # 持久化开关：sqlite 时用落盘仓库(重启不丢 documents/reviews)，
        # 默认 memory 保持测试全离线。向量不进 SQLite(设计红线)。
        self.repository = build_repository(self.settings)
        self.storage = build_file_storage(self.settings)
        self.parser = DocumentParser()
        self.reranker = build_reranker(self.settings)
        self.retriever = HybridRetriever(
            bm25_weight=self.settings.bm25_weight,
            vector_weight=self.settings.vector_weight,
            embedding_dim=self.settings.local_embedding_dim,
            reranker=self.reranker,
            candidate_k=self.settings.retrieval_candidate_k,
        )
        self.llm_gateway = LlmGatewayClient(
            base_url=self.settings.llm_gateway_base_url,
            api_key=self.settings.llm_gateway_api_key,
            user_id=self.settings.llm_gateway_user_id,
            timeout=self.settings.llm_timeout,
        )
        if self.settings.llm_startup_check:
            self.llm_gateway.healthcheck()

        # 只向 Java 网关提交逻辑模型名；厂家路由和 fallback 由网关负责。
        judge = (
            LlmJudge(
                gateway=self.llm_gateway,
                model=self.settings.llm_model,
            )
            if self.settings.llm_enabled
            else None
        )

        # 法规库(双库之一)：按 embedding_provider 选 embedder；启动时加载已建缓存。
        # 缓存不存在(还没建库)时，semantic 检索的法规库为空，reviewer 自动回退旧路径。
        self.embedder = build_embedder(self.settings)
        self.regulation_store = EmbeddingStore()
        self.regulation_store.load(self.settings.regulation_store_path)
        # 缓存是用另一个 embedder 建的(如库是 qwen 建的、当前跑 hash)：向量维度/语义
        # 不同源，相似度会是垃圾值。此时清空并告警，避免静默错误，reviewer 回退旧路径。
        current = getattr(self.embedder, "name", "")
        if len(self.regulation_store) > 0 and self.regulation_store.embedder_name != current:
            log.warning(
                "法规库是用 embedder=%s 建的，当前 embedder=%s 不一致，暂不启用法规库(请重建库)。",
                self.regulation_store.embedder_name or "unknown",
                current,
            )
            self.regulation_store.clear()
        self.indexer = RegulationIndexer(self.embedder)
        self.semantic = SemanticRetriever(self.embedder, self.regulation_store)

        self.reviewer = GmpReviewService(
            repository=self.repository,
            retriever=self.retriever,
            renderer=MarkdownReportRenderer(),
            judge=judge,
            semantic_retriever=self.semantic,
            llm_batch_size=self.settings.llm_batch_size,
        )

        # 跨文档审查同样只经过网关；离线模式不做语义臆测。
        cross_judge = (
            LlmJudge(
                gateway=self.llm_gateway,
                model=self.settings.llm_model,
            )
            if self.settings.llm_enabled
            else None
        )
        self.cross_reviewer = CrossDocumentReviewer(
            semantic_retriever=self.semantic,
            judge=cross_judge,
        )
        # 跨文档快照存储：每次审查冻结成一份可标注、可复算的快照(提交3)。
        self.snapshot_store = SnapshotStore(self.settings.snapshot_dir)

        self.tool_registry = build_business_tool_registry(
            self.repository,
            self.reviewer,
            self.settings.agent_tool_timeout,
        )
        decision_engine = (
            GatewayDecisionEngine(self.llm_gateway, self.settings.agent_model)
            if self.settings.llm_enabled
            else OfflineDecisionEngine()
        )
        graph = AgentGraph(
            decision_engine,
            self.retriever,
            self.repository,
            self.tool_registry,
        )
        self.agent_harness = AgentHarness(graph)
        # Keep the legacy attribute for callers of the original synchronous
        # API; all new execution paths use the Runtime Harness facade.
        self.agent_graph = self.agent_harness

        # 逆向生成也经过网关，复用 reviewer 做"生成即自检"。
        if self._llm_ready():
            generation_gateway = LlmGatewayClient(
                base_url=self.settings.llm_gateway_base_url,
                api_key=self.settings.llm_gateway_api_key,
                user_id=self.settings.llm_gateway_user_id,
                timeout=self.settings.generation_timeout,
            )
            self.chat_client = LlmChatClient(
                gateway=generation_gateway,
                model=self.settings.generation_model,
            )
            self_check_judge = LlmJudge(
                gateway=generation_gateway,
                model=self.settings.generation_model,
            )
            self_check_reviewer = GmpReviewService(
                repository=self.repository,
                retriever=self.retriever,
                renderer=MarkdownReportRenderer(),
                judge=self_check_judge,
                semantic_retriever=self.semantic,
                llm_batch_size=self.settings.llm_batch_size,
            )
            self.generator = DocumentGenerator(
                repository=self.repository,
                semantic_retriever=self.semantic,
                chat_client=self.chat_client,
                reviewer=self_check_reviewer,
            )
        else:
            self.chat_client = None
            self.generator = None

    def _llm_ready(self) -> bool:
        """语义判定和生成共用同一个网关启用开关。"""
        return self.settings.llm_enabled

    def build_regulation_library(self) -> dict:
        """重建法规库：读 PDF → 切块 → embedding → 存缓存，并热更新当前检索库。"""
        stats = self.indexer.build(
            source_dir=self.settings.regulation_source_dir,
            store_path=self.settings.regulation_store_path,
        )
        # 重新加载到当前进程的检索库(无需重启即可生效)。
        self.regulation_store.clear()
        self.regulation_store.load(self.settings.regulation_store_path)
        return stats

    def regulation_stats(self) -> dict:
        """法规库状态：是否已建、片段数、用的哪个 embedder。"""
        return {
            "built": len(self.regulation_store) > 0,
            "total_chunks": len(self.regulation_store),
            "embedder": getattr(self.embedder, "name", "unknown"),
            "store_path": str(self.settings.regulation_store_path),
        }


container = AppContainer()

