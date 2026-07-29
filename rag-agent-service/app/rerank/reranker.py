from __future__ import annotations

from typing import Protocol

import httpx
from opentelemetry import trace

from app.domain.models import Evidence


class Reranker(Protocol):
    def rerank(self, query: str, candidates: list[Evidence], top_k: int) -> list[Evidence]: ...


class CrossEncoderReranker:
    """Lazy local Cross-Encoder adapter; install the `rerank-local` extra to use it."""

    def __init__(self, model_name: str, batch_size: int = 16) -> None:
        self.model_name = model_name
        self.batch_size = batch_size
        self._model = None

    def rerank(self, query: str, candidates: list[Evidence], top_k: int) -> list[Evidence]:
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
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    def rerank(self, query: str, candidates: list[Evidence], top_k: int) -> list[Evidence]:
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
