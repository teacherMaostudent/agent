import logging
import time

log = logging.getLogger(__name__)


class IngestionWorker:
    def __init__(self, container) -> None:
        self.container = container

    def run_once(self) -> bool:
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
        while True:
            if not self.run_once():
                time.sleep(self.container.settings.ingestion_poll_interval)
