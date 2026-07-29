"""阶段 A：多文件临时库 + 双通道召回(提交 1)。

全离线(hash embedder，见 conftest)。验证三点：
1. 多份文件进同一个临时库，每片带 document_id/filename 标签；
2. search_document_set 每份文件保底召回(矛盾双方都在场，解 Q5)；
3. keyword_scan 不受 top_k 限制，能捞到向量可能漏的词(解 Q5/Q6)。
"""
from app.retrieval.embedder import build_embedder
from app.retrieval.embedding_store import EmbeddingStore
from app.retrieval.semantic_retriever import SemanticRetriever
from app.core.config import get_settings


def _retriever() -> SemanticRetriever:
    embedder = build_embedder(get_settings())
    return SemanticRetriever(embedder, EmbeddingStore())


# 两份文件在"洁净度"上打架：A 说 D 级、B 说 B 级。
_FILE_A = "洁净区管理规程\n灌装间应维持 D 级洁净度，静态尘埃粒子数符合 D 级标准。\n仓库为一般生产区。"
_FILE_B = "无菌灌装操作规程\n灌装间应维持 B 级洁净度，动态监测符合 B 级标准。\n复核人不得为操作本人。"


def test_build_set_store_tags_each_chunk() -> None:
    r = _retriever()
    store = r.build_document_set_store(
        [("docA", "A.txt", _FILE_A), ("docB", "B.txt", _FILE_B)]
    )
    assert len(store) >= 2
    # 每片都带来源标签(矛盾检测靠它认出"来自哪份文件")。
    doc_ids = {c.metadata.get("document_id") for c in store._chunks}
    assert doc_ids == {"docA", "docB"}


def test_search_set_recalls_both_files() -> None:
    """每份文件保底召回 → 矛盾双方(A的D级、B的B级)都进结果。"""
    r = _retriever()
    store = r.build_document_set_store(
        [("docA", "A.txt", _FILE_A), ("docB", "B.txt", _FILE_B)]
    )
    hits = r.search_document_set("灌装间 洁净度 等级", store, per_file_k=2)
    files_hit = {ev.metadata.get("document_id") for ev in hits}
    assert files_hit == {"docA", "docB"}  # 双方都在场，否则漏检矛盾


def test_keyword_scan_finds_alias_terms() -> None:
    """关键词全文扫：命中任一 term/别名即捞出上下文，不经向量。"""
    r = _retriever()
    files = [("docA", "A.txt", _FILE_A), ("docB", "B.txt", _FILE_B)]
    hits = r.keyword_scan_document_set(files, terms=["洁净度", "尘埃粒子"])
    files_hit = {ev.metadata.get("document_id") for ev in hits}
    assert files_hit == {"docA", "docB"}
    assert all(ev.metadata.get("channel") == "keyword" for ev in hits)


def test_empty_store_returns_empty() -> None:
    r = _retriever()
    assert r.search_document_set("任意", EmbeddingStore(), per_file_k=2) == []
