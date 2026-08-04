from datetime import UTC, datetime

from app.bootstrap.repository import build_repository
from app.contracts.ingestion import JobStatus
from app.core.config import get_settings
from app.ingestion.job_store import IngestionJobStore
from app.ingestion.parsers import DocumentParser
from app.ingestion.postgres_job_store import PostgresIngestionJobStore
from app.ingestion.processor import IngestionJobProcessor
from app.ingestion.temporal_jobs import TemporalIngestionJobStore
from app.knowledge.regulation_indexer import RegulationIndexer
from app.retrieval.embedder import build_embedder
from app.retrieval.search_projection import build_search_projection
from app.storage.factory import build_file_storage


class IngestionContainer:
    def __init__(self, *, enable_temporal_dispatch: bool = True) -> None:
        self.settings = get_settings()
        self.repository = build_repository(self.settings)
        self.storage = build_file_storage(self.settings)
        self.search_projection = build_search_projection(self.settings)
        self.parser = DocumentParser()
        self.embedder = build_embedder(self.settings)
        self.indexer = RegulationIndexer(self.embedder)
        backing_job_store = (
            PostgresIngestionJobStore(
                self.settings.database_url, self.settings.database_schema
            )
            if self.settings.persistence == "postgres"
            else IngestionJobStore(self.settings.ingestion_jobs_path)
        )
        self.job_store = (
            TemporalIngestionJobStore(
                backing_job_store,
                self.settings.temporal_target,
                self.settings.temporal_namespace,
                self.settings.temporal_ingestion_task_queue,
            )
            if self.settings.temporal_enabled and enable_temporal_dispatch
            else backing_job_store
        )
        self.processor = IngestionJobProcessor(self)

    def execute_job(self, job_id: str) -> dict:
        job = self.job_store.get(job_id)
        if job is None:
            raise ValueError("ingestion job not found")
        job.status = JobStatus.RUNNING
        job.attempts += 1
        job.updated_at = datetime.now(UTC)
        self.job_store._write(job)
        try:
            result = self.processor.process(job)
            self.job_store.complete(job, result)
            return result
        except Exception as exc:
            self.job_store.fail(job, f"{type(exc).__name__}: {exc}")
            raise

    def close(self) -> None:
        self.job_store.close()
        close = getattr(self.repository, "close", None)
        if close is not None:
            close()
