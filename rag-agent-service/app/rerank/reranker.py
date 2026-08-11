from __future__ import annotations

from typing import Protocol

import httpx
from opentelemetry import trace

from app.domain.models import Evidence


class Reranker(Protocol):
    def rerank(self, query: str, candidates: list[Evidence], top_k: int) -> list[Evidence]:
        """在已有 ACL 过滤候选内重排，绝不能自行扩展证据集合。"""
        ...


class CrossEncoderReranker:
    """Lazy local Cross-Encoder adapter; install the `rerank-local` extra to use it."""

    def __init__(self, model_name: str, batch_size: int = 16) -> None:
        """保存惰性加载的本地模型参数，避免 API 进程启动即占用 GPU/内存。"""
        self.model_name = model_name
        self.batch_size = batch_size
        self._model = None

    def rerank(self, query: str, candidates: list[Evidence], top_k: int) -> list[Evidence]:
        """对受控候选执行本地交叉编码精排，并保留召回分以便解释排序变化。"""
        if not candidates:
            return []
        model = self._load_model()
        with trace.get_tracer(__name__).start_as_current_span("rag.rerank.cross_encoder") as span:
            span.set_attribute("rerank.model", self.model_name)
            span.set_attribute("rerank.candidate_count", len(candidates))
            scores = model.predict(
                [(query, item.text) for item in candidates],
                batch_size=self.batch_size,
                show_progress_bar=False,
            )
        for item, score in zip(candidates, scores, strict=True):
            item.metadata["retrieval_score"] = item.score
            item.score = float(score)
            item.metadata["rerank_provider"] = "cross_encoder"
        return sorted(candidates, key=lambda item: item.score, reverse=True)[:top_k]

    def _load_model(self):
        """首次需要时加载可选依赖；缺包明确失败而非静默回退低质量结果。"""
        if self._model is None:
            try:
                from sentence_transformers import CrossEncoder
            except ImportError as exc:
                raise RuntimeError(
                    'Cross-Encoder rerank requires: pip install -e ".[rerank-local]"'
                ) from exc
            self._model = CrossEncoder(self.model_name)
        return self._model


class VendorReranker:
    """HTTP adapter for Cohere-compatible `/rerank` responses."""

    def __init__(self, base_url: str, api_key: str, model: str, timeout: float) -> None:
        """配置兼容 rerank API；密钥仅用于请求头，不写入证据或日志。"""
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    def rerank(self, query: str, candidates: list[Evidence], top_k: int) -> list[Evidence]:
        """调用供应商精排；非法返回索引被丢弃，避免污染已有候选列表。"""
        if not candidates:
            return []
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        with trace.get_tracer(__name__).start_as_current_span("rag.rerank.vendor") as span:
            span.set_attribute("rerank.model", self.model)
            span.set_attribute("rerank.candidate_count", len(candidates))
            response = httpx.post(
                f"{self.base_url}/rerank",
                headers=headers,
                json={
                    "model": self.model,
                    "query": query,
                    "documents": [item.text for item in candidates],
                    "top_n": min(top_k, len(candidates)),
                },
                timeout=self.timeout,
            )
            response.raise_for_status()
            results = response.json().get("results", [])
        reranked: list[Evidence] = []
        for result in results:
            index = int(result["index"])
            if index < 0 or index >= len(candidates):
                continue
            item = candidates[index]
            item.metadata["retrieval_score"] = item.score
            item.score = float(result.get("relevance_score", 0.0))
            item.metadata["rerank_provider"] = "vendor"
            reranked.append(item)
        return reranked[:top_k]


def build_reranker(settings) -> Reranker | None:
    """依据冻结配置选择精排实现；关闭时返回 None，保持召回链路可用。"""
    if settings.rerank_provider == "cross_encoder":
        return CrossEncoderReranker(settings.rerank_model, settings.rerank_batch_size)
    if settings.rerank_provider == "vendor":
        return VendorReranker(
            settings.rerank_base_url,
            settings.rerank_api_key,
            settings.rerank_model,
            settings.rerank_timeout,
        )
    return None
