"""Embedder 抽象:统一"把文本转成向量"的接口,屏蔽 hash / qwen 差异。

- HashEmbedder:本地确定性 embedding(离线、免密钥、测试用),复用原 Hash 逻辑。
- QwenEmbedder:通义 text-embedding-v3(真实语义,建真法规库用)。
- build_embedder():按配置返回其一;qwen 缺密钥时自动回退 hash 并告警。

法规库建库和检索用同一个 embedder,保证查询向量和库向量同源可比。
"""

import logging
from typing import Protocol

from platform_sdk.contracts.rag import EmbeddingContract

from app.core.config import Settings
from app.retrieval.qwen_embedding import QwenEmbeddingClient

log = logging.getLogger(__name__)


class Embedder(Protocol):
    """RAG 内部统一嵌入接口；实现必须公开与向量一致的不可变契约。"""

    contract: EmbeddingContract

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """把文本批量映射到契约声明的同一向量空间。"""
        ...

    def embed(self, text: str) -> list[float]:
        """把单条查询映射到契约声明的同一向量空间。"""
        ...


class HashEmbedder:
    """确定性本地 embedding,与旧 HashEmbeddingRetriever 同算法,离线可用。"""

    def __init__(self, dim: int = 384) -> None:
        """设置稳定哈希向量维度，索引与查询必须使用同一维度。"""
        self.contract = EmbeddingContract(
            provider="local",
            model="deterministic-hash",
            model_revision="sha256-token-v1",
            dimension=dim,
            license="internal-test-only",
            deployment_mode="local",
        )

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """批量生成本地确定性向量，不产生网络调用或供应商费用。"""
        return [self._embed(t) for t in texts]

    def embed(self, text: str) -> list[float]:
        """生成单条查询/片段向量，复用与批量路径一致的算法。"""
        return self._embed(text)

    def _embed(self, text: str) -> list[float]:
        """将 token 映射到哈希桶；该回退仅保障可用性，不提供深层语义理解。"""
        # 保留旧实现，防止本地索引/测试的确定性结果无理由漂移；生产索引应选择语义模型。
        from app.retrieval.vector_retriever import HashEmbeddingRetriever

        return HashEmbeddingRetriever(self.contract.dimension)._embed(text)


class CloudBaselineEmbeddingProvider:
    """DashScope text-embedding-v3 云基线 Provider；它是对比基线而非默认生产裁决。"""

    def __init__(self, client: QwenEmbeddingClient, contract: EmbeddingContract) -> None:
        """注入云端兼容客户端与完整契约，调用方可审计其地区、模型和成本边界。"""
        self.client = client
        self.contract = contract

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """委托远端批量嵌入；传输或配额错误应由上层摄取任务记录并重试。"""
        return _validate_vectors(self.contract, self.client.embed_batch(_prepare_texts(self.contract, texts)))

    def embed(self, text: str) -> list[float]:
        """委托远端单条嵌入，用于查询时与同版本索引比较。"""
        return self.embed_batch([text])[0]


class LocalOpenAiEmbeddingProvider:
    """自部署 OpenAI 兼容嵌入 Provider，可承载 BGE-M3 或 Qwen3-Embedding。"""

    def __init__(self, client: QwenEmbeddingClient, contract: EmbeddingContract) -> None:
        """保存本地服务客户端与模型契约；实际权重、GPU 和网络隔离属于独立部署边界。"""
        self.client = client
        self.contract = contract

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """调用自部署兼容端点批量编码；返回维度随后由查询/索引路径校验。"""
        return _validate_vectors(self.contract, self.client.embed_batch(_prepare_texts(self.contract, texts)))

    def embed(self, text: str) -> list[float]:
        """调用自部署兼容端点编码查询。"""
        return self.embed_batch([text])[0]


def _prepare_texts(contract: EmbeddingContract, texts: list[str]) -> list[str]:
    """在离开服务边界前执行契约中的输入上限与可选指令模板。"""
    prepared: list[str] = []
    for text in texts:
        bounded = text[: contract.max_input_chars]
        prepared.append(contract.instruction_template.format(text=bounded) if contract.instruction_template else bounded)
    return prepared


def _validate_vectors(contract: EmbeddingContract, vectors: list[list[float]]) -> list[list[float]]:
    """拒绝端点返回维度或数量漂移，避免污染已发布的向量空间。"""
    if any(len(vector) != contract.dimension for vector in vectors):
        raise ValueError(f"embedding dimension drift: expected {contract.dimension}")
    return vectors


def build_embedder(settings: Settings) -> Embedder:
    """按冻结配置创建嵌入 Provider；生产语义 Provider 缺凭证时拒绝而非静默换向量空间。"""
    if settings.embedding_provider in {"qwen", "cloud_dashscope"}:
        if not settings.dashscope_api_key:
            if settings.embedding_allow_hash_fallback:
                log.warning("embedding_provider=qwen 缺少密钥，显式允许后回退确定性 hash")
                return HashEmbedder(settings.local_embedding_dim)
            raise ValueError("embedding_provider=qwen requires DASHSCOPE_API_KEY")
        client = QwenEmbeddingClient(
            api_key=settings.dashscope_api_key,
            base_url=settings.qwen_embedding_base_url,
            model=settings.qwen_embedding_model,
            batch_size=settings.qwen_embedding_batch_size,
        )
        return CloudBaselineEmbeddingProvider(
            client,
            EmbeddingContract(
                provider="dashscope",
                model=settings.qwen_embedding_model,
                model_revision=settings.embedding_model_revision,
                dimension=settings.qwen_embedding_dimension,
                normalized=settings.embedding_normalized,
                max_input_chars=settings.embedding_max_input_chars,
                instruction_template=settings.embedding_instruction_template,
                license=settings.embedding_license,
                deployment_mode="cloud",
            ),
        )
    if settings.embedding_provider == "local_openai":
        client = QwenEmbeddingClient(
            api_key=settings.local_embedding_api_key,
            base_url=settings.local_embedding_base_url,
            model=settings.local_embedding_model,
            batch_size=settings.local_embedding_batch_size,
        )
        return LocalOpenAiEmbeddingProvider(
            client,
            EmbeddingContract(
                provider="openai-compatible-local",
                model=settings.local_embedding_model,
                model_revision=settings.embedding_model_revision,
                dimension=settings.local_embedding_dimension,
                normalized=settings.embedding_normalized,
                max_input_chars=settings.embedding_max_input_chars,
                instruction_template=settings.embedding_instruction_template,
                license=settings.local_embedding_license,
                deployment_mode="self_hosted",
            ),
        )
    return HashEmbedder(settings.local_embedding_dim)
