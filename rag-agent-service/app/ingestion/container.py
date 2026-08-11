from datetime import UTC, datetime

from app.bootstrap.repository import build_repository
from app.contracts.ingestion import JobStatus
from app.core.config import get_settings
from app.ingestion.job_store import IngestionJobStore
from app.ingestion.parsers import DocumentParser
from app.ingestion.postgres_job_store import PostgresIngestionJobStore
from app.ingestion.processor import IngestionJobProcessor
from app.ingestion.temporal_jobs import TemporalIngestionJobStore
from app.retrieval.embedder import build_embedder
from app.retrieval.search_projection import build_search_projection
from app.storage.factory import build_file_storage


class IngestionContainer:
    """组装摄取 API、Worker 与 Temporal Activity 共享的依赖对象。"""

    def __init__(self, *, enable_temporal_dispatch: bool = True) -> None:
        """按配置选择持久化队列，并确保 API 进程不会误启动 Temporal 派发。"""
        self.settings = get_settings()
        self.repository = build_repository(self.settings)
        self.storage = build_file_storage(self.settings)
        self.search_projection = build_search_projection(self.settings)
        self.parser = DocumentParser()
        self.embedder = build_embedder(self.settings)
        backing_job_store = (
            PostgresIngestionJobStore(self.settings.database_url, self.settings.database_schema)
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
        """供 Temporal Activity 执行一个已持久化任务，并原子记录完成或失败状态。

        这里不创建任务；通过先读取既有 job_id 保持 API 重试、Temporal 重试和
        审计记录指向同一实体。异常会写回队列后继续抛出，以便 Temporal 应用策略。
        """
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
        """关闭队列与知识库连接；生命周期结束时不再接受新任务。"""
        self.job_store.close()
        close = getattr(self.repository, "close", None)
        if close is not None:
            close()
