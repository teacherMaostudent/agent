import logging
import time

log = logging.getLogger(__name__)


class IngestionWorker:
    """轮询持久化任务队列的本地/开发 Worker。"""

    def __init__(self, container) -> None:
        """保存容器；Worker 不拥有业务配置或队列状态。"""
        self.container = container

    def run_once(self) -> bool:
        """抢占一个租约任务并处理；失败写回队列以触发指数退避，不吞掉状态。"""
        job = self.container.job_store.claim_next()
        if job is None:
            return False
        try:
            result = self.container.processor.process(job)
            self.container.job_store.complete(job, result)
            log.info("ingestion job completed job_id=%s type=%s", job.job_id, job.job_type)
        except Exception as exc:
            self.container.job_store.fail(job, f"{type(exc).__name__}: {exc}")
            log.exception("ingestion job failed job_id=%s type=%s", job.job_id, job.job_type)
        return True

    def run_forever(self) -> None:
        """持续轮询空队列；生产环境优先由 Temporal Worker 承担调度与重试。"""
        while True:
            if not self.run_once():
                time.sleep(self.container.settings.ingestion_poll_interval)
