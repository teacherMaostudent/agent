"""SQLite 持久化仓库测试。

核心验收:重启(新建一个指向同一 db 文件的 repository 实例)后,之前存的
documents/reviews 仍在。用 tmp_path 隔离,不污染真实 data 目录。
"""
from datetime import datetime, timezone

from app.domain.models import Document, ReviewResult, RiskLevel
from app.knowledge.sqlite_repository import SqliteRepository


def _make_document(doc_id: str = "doc_x") -> Document:
    return Document(
        document_id=doc_id,
        filename="测试规程.txt",
        file_path="/tmp/测试规程.txt",
        sha256="deadbeef",
        text="灌装间应维持 D 级洁净度。",
        status="PARSED",
    )


def _make_review(review_id: str = "rev_x") -> ReviewResult:
    return ReviewResult(
        review_id=review_id,
        document_id="doc_x",
        summary="测试审查",
        overall_risk=RiskLevel.LOW,
        dimensions=[],
        report_markdown="# 报告",
        created_at=datetime.now(timezone.utc),
    )


def test_save_and_get_document(tmp_path) -> None:
    repo = SqliteRepository(tmp_path / "gmp.db")
    repo.save_document(_make_document())
    got = repo.get_document("doc_x")
    assert got is not None
    assert got.filename == "测试规程.txt"


def test_document_survives_restart(tmp_path) -> None:
    """核心:存文档 → 新建实例(模拟重启) → 文档仍在。"""
    db = tmp_path / "gmp.db"
    repo1 = SqliteRepository(db)
    repo1.save_document(_make_document("doc_persist"))

    repo2 = SqliteRepository(db)  # 模拟进程重启,重新加载
    got = repo2.get_document("doc_persist")
    assert got is not None
    assert got.text == "灌装间应维持 D 级洁净度。"


def test_review_survives_restart(tmp_path) -> None:
    db = tmp_path / "gmp.db"
    repo1 = SqliteRepository(db)
    repo1.save_review(_make_review("rev_persist"))

    repo2 = SqliteRepository(db)
    got = repo2.get_review("rev_persist")
    assert got is not None
    assert got.summary == "测试审查"


def test_config_logic_inherited(tmp_path) -> None:
    """继承自 InMemoryRepository 的只读配置逻辑照常工作(清单非空)。"""
    repo = SqliteRepository(tmp_path / "gmp.db")
    assert len(repo.checklist) > 0
    assert repo.checklist_for_document_type(None)  # 兜底返回全部


def test_overwrite_document(tmp_path) -> None:
    """同 id 再存 → 覆盖,不重复。"""
    db = tmp_path / "gmp.db"
    repo = SqliteRepository(db)
    repo.save_document(_make_document("doc_dup"))
    updated = _make_document("doc_dup")
    updated.status = "REVIEWED"
    repo.save_document(updated)

    repo2 = SqliteRepository(db)
    assert repo2.get_document("doc_dup").status == "REVIEWED"
