"""Start the split services, exercise both data paths, then shut them down."""

from __future__ import annotations

import os
import tempfile
import threading
import time

import httpx
import uvicorn


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="rag-agent-smoke-") as data_dir:
        os.environ.update(
            {
                "RAG_DATA_DIR": data_dir,
                "RAG_PERSISTENCE": "sqlite",
                "RAG_LLM_ENABLED": "false",
                "RAG_LLM_STARTUP_CHECK": "false",
                "RAG_CONTEXT_SERVICE_BASE_URL": "http://127.0.0.1:18002",
                "RAG_RAG_QUERY_BASE_URL": "http://127.0.0.1:18003",
            }
        )

        # Imports intentionally happen after environment setup because each module
        # constructs its own process-level composition root.
        from app.ingestion.worker import IngestionWorker
        from apps.agent_context_service.main import app as context_app
        from agent_runtime_service.main import app as runtime_app
        from apps.ingestion_api.main import app as ingestion_app
        from apps.rag_query_api.main import app as query_app

        services = [
            _Server(query_app, 18003),
            _Server(context_app, 18002),
            _Server(runtime_app, 18001),
            _Server(ingestion_app, 18004),
        ]
        try:
            for service in services:
                service.start()
            for port in (18001, 18002, 18003, 18004):
                _wait_ready(port)

            query = httpx.post(
                "http://127.0.0.1:18003/api/v1/query/search",
                json={"query": "audit record retention", "top_k": 3},
                timeout=10,
            )
            query.raise_for_status()

            runtime = httpx.post(
                "http://127.0.0.1:18001/api/v1/agent/run",
                headers={
                    "X-Tenant-Id": "tenant-a",
                    "X-User-Id": "user-a",
                    "X-Permissions": "rag:read",
                },
                json={
                    "task": "Find the audit record requirement",
                    "session_id": "smoke-session",
                    "max_steps": 4,
                },
                timeout=20,
            )
            runtime.raise_for_status()

            upload = httpx.post(
                "http://127.0.0.1:18004/api/v1/ingestion/documents",
                headers={"X-Tenant-Id": "tenant-a", "X-User-Id": "user-a"},
                files={
                    "file": ("smoke.md", b"# Smoke\nAudit records are retained.", "text/markdown")
                },
                timeout=10,
            )
            upload.raise_for_status()
            upload_body = upload.json()
            IngestionWorker(ingestion_app.state.container).run_once()
            job = httpx.get(
                f"http://127.0.0.1:18004/api/v1/ingestion/jobs/{upload_body['job']['job_id']}",
                timeout=10,
            )
            job.raise_for_status()
            if job.json()["status"] != "COMPLETED":
                raise RuntimeError(f"ingestion did not complete: {job.text}")

            print(
                "distributed smoke passed:",
                {
                    "rag_results": len(query.json()["evidence"]),
                    "agent_steps": runtime.json()["steps"],
                    "ingestion_status": job.json()["status"],
                },
            )
        finally:
            for service in reversed(services):
                service.stop()


class _Server:
    def __init__(self, app, port: int) -> None:
        self.server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
        )
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=5)


def _wait_ready(port: int) -> None:
    url = f"http://127.0.0.1:{port}/api/v1/health"
    for _ in range(50):
        try:
            response = httpx.get(url, timeout=1)
            if response.status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.1)
    raise RuntimeError(f"service did not become ready: {url}")


if __name__ == "__main__":
    main()
