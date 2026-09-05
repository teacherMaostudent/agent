"""Approved Wiki versions must become real parse jobs, not decorative reindex events."""

import hashlib
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.service_api.ingestion_api import router


def test_approved_wiki_page_is_idempotently_persisted_and_queued(tmp_path: Path) -> None:
    markdown = "# Refund rule\n\nHuman-confirmed content.\n"
    digest = hashlib.sha256(markdown.encode()).hexdigest()

    class Storage:
        def save_upload(self, filename, stream: BytesIO):
            path = tmp_path / filename
            content = stream.read()
            path.write_bytes(content)
            return path, hashlib.sha256(content).hexdigest()

        @staticmethod
        def object_key_for(_path):
            return None

    class Repository:
        def __init__(self):
            self.documents = {}

        def get_document(self, document_id):
            return self.documents.get(document_id)

        def save_document(self, document):
            self.documents[document.document_id] = document
            return document

    class JobStore:
        def __init__(self):
            self.jobs = {}

        def get(self, job_id, tenant_id):
            job = self.jobs.get(job_id)
            return job if job and job.tenant_id == tenant_id else None

        def create(self, job):
            self.jobs[job.job_id] = job
            return job

    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    repository = Repository()
    jobs = JobStore()
    app.state.container = SimpleNamespace(
        storage=Storage(), repository=repository, job_store=jobs
    )
    request = {
        "page_id": "wiki-1",
        "candidate_id": "candidate-1",
        "version": 1,
        "title": "Refund rule",
        "markdown": markdown,
        "content_sha256": digest,
        "approved_by": "expert-1",
        "source_ids": ["evidence-1", "review-1"],
        "supersedes_page_ids": ["wiki-old"],
    }
    with TestClient(app) as client:
        first = client.post(
            "/api/v1/ingestion/wiki-pages",
            headers={"X-Tenant-Id": "tenant-a", "X-User-Id": "wiki-relay"},
            json=request,
        )
        second = client.post(
            "/api/v1/ingestion/wiki-pages",
            headers={"X-Tenant-Id": "tenant-a", "X-User-Id": "wiki-relay"},
            json=request,
        )
    assert first.status_code == 202, first.text
    assert second.json() == first.json()
    assert len(repository.documents) == 1
    assert len(jobs.jobs) == 1
    assert next(iter(jobs.jobs.values())).job_type == "PARSE"
    assert next(iter(repository.documents.values())).metadata["supersedes_page_ids"] == ["wiki-old"]


def test_wiki_ingestion_rejects_content_digest_mismatch(tmp_path: Path) -> None:
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.state.container = SimpleNamespace(storage=None, repository=None, job_store=None)
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/ingestion/wiki-pages",
            json={
                "page_id": "wiki-1",
                "candidate_id": "candidate-1",
                "version": 1,
                "title": "Rule",
                "markdown": "different",
                "content_sha256": "0" * 64,
                "approved_by": "expert-1",
                "source_ids": ["evidence-1"],
            },
        )
    assert response.status_code == 422
