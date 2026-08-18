"""以 Golden Retrieval Dataset 对候选嵌入 Provider 做可复现离线比较。"""

from __future__ import annotations

from dataclasses import dataclass
from time import perf_counter

from app.domain.models import Chunk
from app.retrieval.embedder import Embedder
from app.retrieval.vector_retriever import EmbeddingVectorRetriever


@dataclass(frozen=True)
class GoldenRetrievalCase:
    """一个固定查询、候选语料与期望证据集合；可由 Governance Golden Case 导出。"""

    case_id: str
    query: str
    chunks: tuple[Chunk, ...]
    expected_source_ids: frozenset[str]
    criticality: str = "normal"


@dataclass(frozen=True)
class EmbeddingBenchmarkReport:
    """候选 Provider 的可审计对比结果，不能单凭平均 Recall 自动提升生产别名。"""

    provider_contract_id: str
    model: str
    recall_at_k: float
    false_negative_rate: float
    p95_latency_ms: float
    estimated_cost_usd: float
    deployment_mode: str
    license: str
    high_risk_recall_at_k: float


class EmbeddingBenchmarkRunner:
    """在相同 Golden Case、Top-K 和成本假设下评测 Cloud Baseline 与本地 Provider。"""

    def __init__(self, cases: tuple[GoldenRetrievalCase, ...], *, top_k: int = 20) -> None:
        """冻结用例顺序和 Top-K，避免候选模型通过改变评测条件取得表面优势。"""
        if not cases or top_k < 1:
            raise ValueError("embedding benchmark requires cases and a positive top_k")
        self._cases = cases
        self._top_k = top_k

    def run(
        self, provider: Embedder, *, estimated_cost_per_million_chars_usd: float = 0.0
    ) -> EmbeddingBenchmarkReport:
        """执行纯向量召回基准，报告漏检、P95 延迟、成本估计与部署合规事实。"""
        retriever = EmbeddingVectorRetriever(provider)
        recalls: list[float] = []
        high_risk_recalls: list[float] = []
        latencies: list[float] = []
        total_chars = 0
        for case in self._cases:
            started = perf_counter()
            hits = retriever.search(case.query, list(case.chunks), self._top_k)
            latencies.append((perf_counter() - started) * 1_000)
            returned = {item.source_id for item in hits}
            expected = case.expected_source_ids
            recall = len(returned & expected) / len(expected) if expected else 1.0
            recalls.append(recall)
            if case.criticality.lower() in {"high", "critical"}:
                high_risk_recalls.append(recall)
            total_chars += len(case.query) + sum(len(chunk.text) for chunk in case.chunks)
        ordered_latency = sorted(latencies)
        p95_index = max(0, int(len(ordered_latency) * 0.95) - 1)
        recall = sum(recalls) / len(recalls)
        contract = provider.contract
        return EmbeddingBenchmarkReport(
            provider_contract_id=contract.contract_id,
            model=contract.model,
            recall_at_k=recall,
            false_negative_rate=1.0 - recall,
            p95_latency_ms=ordered_latency[p95_index],
            estimated_cost_usd=(total_chars / 1_000_000) * estimated_cost_per_million_chars_usd,
            deployment_mode=contract.deployment_mode,
            license=contract.license,
            high_risk_recall_at_k=(sum(high_risk_recalls) / len(high_risk_recalls))
            if high_risk_recalls
            else recall,
        )
