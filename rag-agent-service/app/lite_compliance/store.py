from __future__ import annotations

from pathlib import Path

from app.lite_compliance.models import (
    FeedbackInput,
    HistoryEvent,
    LiteDocument,
    RegulationClause,
    ReviewJob,
    new_id,
)
from app.storage.sqlite_kv import SqliteKv


class LiteComplianceStore:
    """Small single-node store for the feasibility phase.

    It intentionally reuses the existing SQLite KV layer. Search indexes, Kafka,
    Temporal, Control Plane and Governance are not required for this process.
    """

    DOCUMENT = "lite_document"
    CLAUSE = "lite_clause"
    JOB = "lite_review_job"
    EVENT = "lite_history_event"
    FEEDBACK = "lite_feedback"

    def __init__(self, path: Path) -> None:
        self.db = SqliteKv(path)

    def close(self) -> None:
        self.db.close()

    def put_document(self, document: LiteDocument) -> LiteDocument:
        self.db.put(self.DOCUMENT, document.document_id, document.model_dump(mode="json"))
        return document

    def put_documents(self, documents: list[LiteDocument]) -> None:
        self.db.put_many(
            self.DOCUMENT,
            [
                (document.document_id, document.model_dump(mode="json"))
                for document in documents
            ],
        )

    def documents(self, ids: list[str] | None = None) -> list[LiteDocument]:
        wanted = set(ids or [])
        items = [LiteDocument.model_validate(item) for item in self.db.all(self.DOCUMENT)]
        return [item for item in items if not wanted or item.document_id in wanted]

    def put_clause(self, clause: RegulationClause) -> RegulationClause:
        self.db.put(self.CLAUSE, clause.clause_id, clause.model_dump(mode="json"))
        return clause

    def clauses(self, ids: list[str] | None = None) -> list[RegulationClause]:
        wanted = set(ids or [])
        items = [RegulationClause.model_validate(item) for item in self.db.all(self.CLAUSE)]
        return [item for item in items if not wanted or item.clause_id in wanted]

    def put_job(self, job: ReviewJob) -> ReviewJob:
        self.db.put(self.JOB, job.job_id, job.model_dump(mode="json"))
        return job

    def get_job(self, job_id: str) -> ReviewJob | None:
        payload = self.db.get(self.JOB, job_id)
        return ReviewJob.model_validate(payload) if payload else None

    def jobs(self) -> list[ReviewJob]:
        jobs = [ReviewJob.model_validate(item) for item in self.db.all(self.JOB)]
        return sorted(jobs, key=lambda item: item.created_at, reverse=True)

    def add_event(self, event: HistoryEvent) -> HistoryEvent:
        self.db.put(self.EVENT, event.event_id, event.model_dump(mode="json"))
        return event

    def add_events(self, events: list[HistoryEvent]) -> None:
        self.db.put_many(
            self.EVENT,
            [(event.event_id, event.model_dump(mode="json")) for event in events],
        )

    def events(self, event_type: str | None = None, object_id: str | None = None) -> list[HistoryEvent]:
        items = [HistoryEvent.model_validate(item) for item in self.db.all(self.EVENT)]
        if event_type:
            items = [item for item in items if item.event_type == event_type]
        if object_id:
            items = [item for item in items if item.object_id == object_id]
        return sorted(items, key=lambda item: item.occurred_at, reverse=True)

    def add_feedback(self, feedback: FeedbackInput) -> dict:
        feedback_id = new_id("feedback")
        payload = {"feedback_id": feedback_id, **feedback.model_dump(mode="json")}
        self.db.put(self.FEEDBACK, feedback_id, payload)
        return payload
