from app.bootstrap.repository import build_repository
from app.core.config import get_settings
from app.ingestion.job_store import IngestionJobStore
from app.ingestion.parsers import DocumentParser
from app.ingestion.processor import IngestionJobProcessor
from app.knowledge.regulation_indexer import RegulationIndexer
from app.retrieval.embedder import build_embedder
from app.storage.local_storage import LocalFileStorage


class IngestionContainer:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.repository = build_repository(self.settings)
        self.storage = LocalFileStorage(self.settings)
        self.parser = DocumentParser()
        self.embedder = build_embedder(self.settings)
        self.indexer = RegulationIndexer(self.embedder)
        self.job_store = IngestionJobStore(self.settings.ingestion_jobs_path)
        self.processor = IngestionJobProcessor(self)

    def close(self) -> None:
        self.job_store.close()
        close = getattr(self.repository, "close", None)
        if close is not None:
            close()
