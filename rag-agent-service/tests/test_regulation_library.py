"""法规库 + 双库检索测试(离线，用 hash embedder，不调通义 API)。"""

from app.core.config import Settings
from app.domain.models import Chunk
from app.retrieval.embedder import HashEmbedder
from app.retrieval.embedding_store import EmbeddingStore
from app.retrieval.semantic_retriever import SemanticRetriever


def _seed_regulation_store(embedder: HashEmbedder) -> EmbeddingStore:
    store = EmbeddingStore()
    chunks = [
        Chunk(
            source_id="中国GMP-2010",
            source_type="regulation",
            text="企业应建立偏差处理操作规程，规定偏差的报告、记录、调查和纠正措施。",
            metadata={"standard": "中国GMP-2010", "regulation": "中国药品生产质量管理规范"},
        ),
        Chunk(
            source_id="FDA-cGMP-211",
            source_type="regulation",
            text="Any deviation shall be recorded and investigated before release of the product.",
            metadata={"standard": "FDA-cGMP-211", "regulation": "21 CFR Part 211"},
        ),
    ]
    vectors = embedder.embed_batch([c.text for c in chunks])
    store.add(chunks, vectors)
    return store


def test_embedding_store_save_load(tmp_path) -> None:
    embedder = HashEmbedder(64)
    store = _seed_regulation_store(embedder)
    path = tmp_path / "store.json"
    store.save(path)

    reloaded = EmbeddingStore()
    assert reloaded.load(path) is True
    assert len(reloaded) == 2


def test_store_records_embedder_name(tmp_path) -> None:
    """存/取时应记录建库用的 embedder 名，供检索前一致性校验(防跨 embedder 静默错配)。"""
    embedder = HashEmbedder(64)
    store = EmbeddingStore()
    chunks = [Chunk(source_id="r1", source_type="regulation", text="偏差应记录调查", metadata={})]
    store.add(chunks, embedder.embed_batch([c.text for c in chunks]), embedder_name="hash")
    assert store.embedder_name == "hash"

    path = tmp_path / "store.json"
    store.save(path)
    reloaded = EmbeddingStore()
    reloaded.load(path)
    assert reloaded.embedder_name == "hash"


def test_semantic_retriever_dual_library() -> None:
    embedder = HashEmbedder(128)
    reg_store = _seed_regulation_store(embedder)
    retriever = SemanticRetriever(embedder, reg_store)

    assert retriever.has_regulation_library() is True

    # 法规库检索：偏差查询应召回偏差条文。
    reg_hits = retriever.search_regulations("偏差 记录 调查", top_k=2)
    assert reg_hits
    assert any("偏差" in h.text for h in reg_hits)

    # 企业文件临时库：切块 + 检索。
    doc_store = retriever.build_document_store(
        "doc1", "本公司偏差管理规程规定：偏差应记录、调查并采取纠正措施。"
    )
    assert len(doc_store) >= 1
    doc_hits = retriever.search_document("偏差 调查", doc_store, top_k=2)
    assert doc_hits


def test_empty_regulation_library_reports_false() -> None:
    embedder = HashEmbedder(64)
    retriever = SemanticRetriever(embedder, EmbeddingStore())
    assert retriever.has_regulation_library() is False
    assert retriever.search_regulations("任意查询") == []


def test_dashscope_api_key_accepts_bare_env(monkeypatch) -> None:
    """裸 DASHSCOPE_API_KEY 应被 Settings 接受(用户已有的变量可直接用)。"""
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-test-bare")
    settings = Settings()
    assert settings.dashscope_api_key == "sk-test-bare"
