from __future__ import annotations

import logging

from pydantic import ValidationError

from app.domain.models import Document, ReviewResult
from app.knowledge.repository import InMemoryRepository

_KIND_DOCUMENT = "document"
_KIND_REVIEW = "review"
log = logging.getLogger(__name__)


class DurableRepository(InMemoryRepository):
    def __init__(self, kv) -> None:
        super().__init__()
        self._kv = kv
        self._load_all()

    def _load_all(self) -> None:
        for payload in self._kv.all(_KIND_DOCUMENT):
            try:
                document = Document(**payload)
                self.documents[document.document_id] = document
            except ValidationError:
                log.warning("invalid persisted document ignored")
        for payload in self._kv.all(_KIND_REVIEW):
            try:
                review = ReviewResult(**payload)
                self.reviews[review.review_id] = review
            except ValidationError:
                log.warning("invalid persisted review ignored")

    def save_document(self, document: Document) -> Document:
        super().save_document(document)
        self._kv.put(_KIND_DOCUMENT, document.document_id, document.model_dump(mode="json"))
        return document

    def get_document(self, document_id: str) -> Document | None:
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
