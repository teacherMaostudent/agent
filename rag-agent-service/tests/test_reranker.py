from app.domain.models import Chunk
from app.rerank.reranker import CrossEncoderReranker
from app.retrieval.hybrid_retriever import HybridRetriever


class FakeCrossEncoder:
    def predict(self, pairs, batch_size, show_progress_bar):
        return [0.1 if "irrelevant" in text else 0.9 for _, text in pairs]


def test_cross_encoder_changes_hybrid_order() -> None:
    reranker = CrossEncoderReranker("fake-model")
    reranker._model = FakeCrossEncoder()
    retriever = HybridRetriever(0.5, 0.5, 32, reranker=reranker, candidate_k=4)
    chunks = [
        Chunk(source_id="bad", source_type="regulation", text="audit audit irrelevant"),
        Chunk(source_id="good", source_type="regulation", text="audit record retention requirement"),
    ]

    hits = retriever.search("audit", chunks, top_k=2)

    assert hits[0].source_id == "good"
    assert hits[0].metadata["rerank_provider"] == "cross_encoder"
