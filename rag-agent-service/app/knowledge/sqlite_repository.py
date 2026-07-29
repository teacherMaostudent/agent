"""SQLite 持久化仓库(持久化基础设施)。

设计(对应设计文档第7节)：
- 继承 InMemoryRepository，复用其全部只读配置逻辑(清单/术语/分类过滤/切块)，
  只重写 4 个【可变】方法(save/get document、save/get review)做写穿透 + 启动加载。
- 作用域仅 documents + reviews——这俩是原本重启会丢的。跨文档快照(SnapshotStore)、
  法规向量(EmbeddingStore) 已各自 JSON 持久，不重复迁移。
- **向量绝不进 SQLite**(红线)：document_chunks 仍在内存按需切块，不落库。
- 启动时把已存的 documents 载回 self.documents，保证 document_chunks 等依赖内存
  字典的方法照常工作。
"""
from pathlib import Path

from app.domain.models import Document, ReviewResult
from app.knowledge.repository import InMemoryRepository
from app.storage.sqlite_kv import SqliteKv

_KIND_DOCUMENT = "document"
_KIND_REVIEW = "review"


class SqliteRepository(InMemoryRepository):
    def __init__(self, db_path: Path) -> None:
        super().__init__()
        self._kv = SqliteKv(db_path)
        self._load_all()

    def _load_all(self) -> None:
        """启动时把已落盘的 documents/reviews 载回内存字典。"""
        for payload in self._kv.all(_KIND_DOCUMENT):
            try:
                doc = Document(**payload)
                self.documents[doc.document_id] = doc
            except Exception:
                continue  # 单条损坏不影响其余
        for payload in self._kv.all(_KIND_REVIEW):
            try:
                review = ReviewResult(**payload)
                self.reviews[review.review_id] = review
            except Exception:
                continue

    def save_document(self, document: Document) -> Document:
        super().save_document(document)  # 写内存(供 document_chunks 等复用)
        self._kv.put(_KIND_DOCUMENT, document.document_id, document.model_dump(mode="json"))
        return document

    def get_document(self, document_id: str) -> Document | None:
        # Separate API/worker/query processes have independent memory. Read through
        # SQLite so a newly completed ingestion job is immediately visible online.
        payload = self._kv.get(_KIND_DOCUMENT, document_id)
        if payload is None:
            return None
        document = Document(**payload)
        self.documents[document.document_id] = document
        return document

    def document_chunks(self, document_id: str):
        document = self.get_document(document_id)
        if document is None:
            return []
        return self._chunker.chunk(
            document.document_id,
            "enterprise_document",
            document.text,
            document.metadata,
        )

    def save_review(self, review: ReviewResult) -> ReviewResult:
        super().save_review(review)
        self._kv.put(_KIND_REVIEW, review.review_id, review.model_dump(mode="json"))
        return review

    def get_review(self, review_id: str) -> ReviewResult | None:
        payload = self._kv.get(_KIND_REVIEW, review_id)
        if payload is None:
            return None
        review = ReviewResult(**payload)
        self.reviews[review.review_id] = review
        return review

    def close(self) -> None:
        self._kv.close()
