"""End-to-end contract tests for governed Wiki promotion."""

import hashlib

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.relay import WikiRelay


def source(source_id: str, level: str = "raw_evidence") -> dict:
    return {
        "source_id": source_id,
        "source_type": "evidence" if level == "raw_evidence" else "run",
        "knowledge_level": level,
        "content_sha256": hashlib.sha256(source_id.encode()).hexdigest(),
    }


def candidate(client: TestClient, title: str, *, supersedes: str | None = None) -> dict:
    payload = {
        "root_task_id": "root-1",
        "conclusion": f"confirmed conclusion for {title}",
        "sources": [source("ev-1"), source("inference-1", "model_inference")],
        "drafts": [{
            "title": title, "page_type": "rule", "summary": f"summary {title}",
            "body": f"body {title}{' updated' if supersedes else ''}",
            "tags": ["shared", title.lower()],
            "supersedes_page_id": supersedes,
        }],
    }
    response = client.post("/v1/wiki/candidates", headers=headers(), json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def headers(*, reviewer: bool = False) -> dict[str, str]:
    return {
        "X-Tenant-Id": "tenant-a", "X-User-Id": "reviewer" if reviewer else "agent-runtime",
        "X-Roles": "knowledge-reviewer" if reviewer else "",
        "X-Knowledge-Wiki-Key": "wiki-key",
    }


def test_human_review_promotes_and_builds_provenance_conflict_supersede(tmp_path) -> None:
    app = create_app(
        Settings(
            database_url=f"sqlite:///{tmp_path / 'wiki.db'}", service_api_key="wiki-key"
        )
    )
    with TestClient(app) as client:
        first = candidate(client, "Refund Rule")
        queue = client.get(
            "/v1/wiki/candidates", headers=headers(reviewer=True)
        )
        assert queue.status_code == 200
        assert [item["candidate_id"] for item in queue.json()] == [first["candidate_id"]]
        denied = client.post(
            f"/v1/wiki/candidates/{first['candidate_id']}/review", headers=headers(),
            json={"decision": "approve", "comment": "expert checked"},
        )
        assert denied.status_code == 403
        approved = client.post(
            f"/v1/wiki/candidates/{first['candidate_id']}/review", headers=headers(reviewer=True),
            json={"decision": "approve", "comment": "expert checked"},
        )
        assert approved.status_code == 200, approved.text
        page = approved.json()["pages"][0]
        assert page["knowledge_level"] == "human_confirmed"
        assert {item["knowledge_level"] for item in page["sources"]} == {
            "raw_evidence", "model_inference", "human_confirmed"
        }
        assert len(approved.json()["outbox_event_ids"]) == 4

        second = candidate(client, "Refund Rule", supersedes=page["page_id"])
        promoted = client.post(
            f"/v1/wiki/candidates/{second['candidate_id']}/review", headers=headers(reviewer=True),
            json={"decision": "approve", "comment": "new policy supersedes old"},
        ).json()["pages"][0]
        relations = {item["relation_type"] for item in promoted["relations"]}
        assert {"conflicts_with", "supersedes"}.issubset(relations)
        pages = client.get("/v1/wiki/pages", headers=headers()).json()
        old = next(item for item in pages if item["page_id"] == page["page_id"])
        assert old["status"] == "superseded"


def test_raw_evidence_is_mandatory_and_review_is_single_consumption(tmp_path) -> None:
    app = create_app(
        Settings(
            database_url=f"sqlite:///{tmp_path / 'wiki.db'}", service_api_key="wiki-key"
        )
    )
    with TestClient(app) as client:
        invalid = client.post(
            "/v1/wiki/candidates", headers=headers(),
            json={"root_task_id": "r", "conclusion": "x",
                  "sources": [source("i", "model_inference")],
                  "drafts": [{"title": "Only inference", "page_type": "concept",
                              "summary": "s", "body": "b"}]},
        )
        assert invalid.status_code == 422
        item = candidate(client, "Inventory Rule")
        path = f"/v1/wiki/candidates/{item['candidate_id']}/review"
        rejected = client.post(
            path,
            headers=headers(reviewer=True),
            json={"decision": "reject", "comment": "insufficient"},
        )
        assert rejected.status_code == 200
        repeated = client.post(path, headers=headers(reviewer=True),
                               json={"decision": "approve", "comment": "retry"})
        assert repeated.status_code == 409


def test_outbox_relay_triggers_reindex_audit_and_pending_gate(tmp_path, monkeypatch) -> None:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'wiki.db'}", service_api_key="wiki-key",
        governance_event_key="event-key", governance_auditor_key="auditor-key",
    )
    app = create_app(settings)
    with TestClient(app) as client:
        item = candidate(client, "Relay Rule")
        client.post(
            f"/v1/wiki/candidates/{item['candidate_id']}/review",
            headers=headers(reviewer=True),
            json={"decision": "approve", "comment": "reviewed"},
        )
        calls: list[tuple[str, dict]] = []

        class Response:
            def __init__(self, body):
                self.body = body

            def raise_for_status(self):
                return None

            def json(self):
                return self.body

        def post(url, **kwargs):
            calls.append((url, kwargs.get("json") or {}))
            return Response({"job_id": "job-reindex-1"})

        relay = WikiRelay(settings, app.state.repository)
        monkeypatch.setattr(relay, "_post", post)
        assert relay.run_once() == 4
        relay.close()
        assert any(url.endswith("/ingestion/wiki-pages") for url, _ in calls)
        ingestion = next(body for url, body in calls if url.endswith("/ingestion/wiki-pages"))
        assert ingestion["markdown"].startswith("# Relay Rule")
        gate = next(body for url, body in calls if url.endswith("/knowledge-change-gates"))
        assert gate["reindexJobId"] == "job-reindex-1"
        assert app.state.repository.pending_events(10) == []


def test_outbox_failure_is_retried_then_bounded_in_dlq(tmp_path, monkeypatch) -> None:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'wiki.db'}",
        service_api_key="wiki-key",
        relay_max_attempts=1,
    )
    app = create_app(settings)
    with TestClient(app) as client:
        item = candidate(client, "Retry Rule")
        client.post(
            f"/v1/wiki/candidates/{item['candidate_id']}/review",
            headers=headers(reviewer=True),
            json={"decision": "approve", "comment": "reviewed"},
        )

        def fail(*_args, **_kwargs):
            raise ValueError("downstream unavailable")

        relay = WikiRelay(settings, app.state.repository)
        monkeypatch.setattr(relay, "_post", fail)
        assert relay.run_once() == 0
        relay.close()
        dead = app.state.repository.dlq_events()
        assert len(dead) == 4
        assert all(item.last_error for item in dead)
        assert any(item.last_error == "ValueError: downstream unavailable" for item in dead)


def test_release_gate_is_blocked_without_durable_rag_receipt(tmp_path, monkeypatch) -> None:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'wiki.db'}",
        service_api_key="wiki-key",
        relay_max_attempts=1,
    )
    app = create_app(settings)
    with TestClient(app) as client:
        item = candidate(client, "Receipt Rule")
        client.post(
            f"/v1/wiki/candidates/{item['candidate_id']}/review",
            headers=headers(reviewer=True),
            json={"decision": "approve", "comment": "reviewed"},
        )
        calls: list[str] = []

        class Response:
            @staticmethod
            def raise_for_status():
                return None

            @staticmethod
            def json():
                return {}

        def post(url, **_kwargs):
            calls.append(url)
            if url.endswith("/ingestion/wiki-pages"):
                raise ValueError("RAG unavailable")
            return Response()

        relay = WikiRelay(settings, app.state.repository)
        monkeypatch.setattr(relay, "_post", post)
        relay.run_once()
        relay.close()
        assert not any(url.endswith("/knowledge-change-gates") for url in calls)
        dead_types = {item.event_type for item in app.state.repository.dlq_events()}
        assert "wiki.rag.reindex.requested" in dead_types
        assert "wiki.release_gate.requested" in dead_types
