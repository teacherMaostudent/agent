"""Transactional SQL repository shared by local SQLite and production PostgreSQL."""

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from app.models import OutboxEvent, WikiCandidate, WikiPage


class WikiRepository:
    """Persist candidates, immutable page versions and the promotion Outbox atomically."""

    def __init__(self, database_url: str) -> None:
        """Create the SQL pool and bootstrap the new service schema before accepting traffic."""
        url = database_url.replace("postgresql://", "postgresql+psycopg://", 1)
        parsed = make_url(url)
        if parsed.drivername == "sqlite" and parsed.database not in {None, "", ":memory:"}:
            # Local/test mode owns its SQLite parent directory. Production validation rejects
            # SQLite entirely, so this convenience cannot weaken the distributed deployment.
            Path(parsed.database).expanduser().parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_engine(url, pool_pre_ping=True)
        self._create_schema()

    def _create_schema(self) -> None:
        """Create service tables once, including during concurrent first-replica startup.

        PostgreSQL's ``CREATE TABLE IF NOT EXISTS`` does not serialize concurrent catalog
        writes: two fresh API/Relay replicas can still race while creating the same relation
        type.  A transaction-scoped advisory lock makes schema bootstrap single-writer without
        introducing a permanent coordinator; SQLite already serializes schema changes locally.
        """
        statements = (
            """CREATE TABLE IF NOT EXISTS wiki_candidates(
            tenant_id VARCHAR(100) NOT NULL, candidate_id VARCHAR(160) NOT NULL,
            status VARCHAR(40) NOT NULL, payload TEXT NOT NULL,
            PRIMARY KEY(tenant_id, candidate_id))""",
            """CREATE TABLE IF NOT EXISTS wiki_pages(
            tenant_id VARCHAR(100) NOT NULL, page_id VARCHAR(160) NOT NULL,
            canonical_key VARCHAR(240) NOT NULL, version INTEGER NOT NULL,
            status VARCHAR(40) NOT NULL, payload TEXT NOT NULL,
            PRIMARY KEY(tenant_id, page_id), UNIQUE(tenant_id, canonical_key, version))""",
            """CREATE TABLE IF NOT EXISTS wiki_outbox(
            event_id VARCHAR(160) PRIMARY KEY, tenant_id VARCHAR(100) NOT NULL,
            event_type VARCHAR(160) NOT NULL, attempts INTEGER NOT NULL,
            status VARCHAR(40) NOT NULL, next_attempt_at VARCHAR(80) NOT NULL,
            lease_until VARCHAR(80), lease_token VARCHAR(160), last_error TEXT,
            delivered_at VARCHAR(80), payload TEXT NOT NULL, created_at VARCHAR(80) NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS wiki_delivery_receipts(
            tenant_id VARCHAR(100) NOT NULL, page_id VARCHAR(160) NOT NULL,
            version INTEGER NOT NULL, reindex_job_id VARCHAR(160) NOT NULL,
            created_at VARCHAR(80) NOT NULL,
            PRIMARY KEY(tenant_id,page_id,version))""",
        )
        with self.engine.begin() as connection:
            if self.engine.dialect.name == "postgresql":
                # Stable service-specific lock ID. It is intentionally independent of process
                # identity so every Wiki replica competes for the same short-lived DDL lease.
                connection.execute(text("SELECT pg_advisory_xact_lock(879240911)"))
            for statement in statements:
                connection.execute(text(statement))

    @staticmethod
    def _dump(model) -> str:
        """Serialize validated models deterministically for audit-friendly SQL payloads."""
        return json.dumps(model.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)

    def create_candidate(self, candidate: WikiCandidate) -> WikiCandidate:
        """Insert a new tenant-scoped candidate; duplicate identity fails instead of overwriting."""
        with self.engine.begin() as connection:
            connection.execute(
                text("INSERT INTO wiki_candidates VALUES(:tenant,:id,:status,:payload)"),
                {"tenant": candidate.tenant_id, "id": candidate.candidate_id,
                 "status": candidate.status.value, "payload": self._dump(candidate)},
            )
        return candidate

    def get_candidate(self, tenant_id: str, candidate_id: str) -> WikiCandidate | None:
        """Read by the compound tenant/candidate boundary to prevent cross-tenant enumeration."""
        with self.engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT payload FROM wiki_candidates "
                    "WHERE tenant_id=:tenant AND candidate_id=:id"
                ),
                {"tenant": tenant_id, "id": candidate_id},
            ).mappings().first()
        return WikiCandidate.model_validate_json(row["payload"]) if row else None

    def candidates(
        self, tenant_id: str, *, status: str = "", limit: int = 50
    ) -> list[WikiCandidate]:
        """List a bounded same-tenant review queue with an optional validated status filter."""
        query = "SELECT payload FROM wiki_candidates WHERE tenant_id=:tenant"
        values: dict[str, Any] = {"tenant": tenant_id, "limit": min(max(limit, 1), 100)}
        if status:
            query += " AND status=:status"
            values["status"] = status
        query += " ORDER BY candidate_id DESC LIMIT :limit"
        with self.engine.connect() as connection:
            rows = connection.execute(text(query), values).mappings().all()
        return [WikiCandidate.model_validate_json(row["payload"]) for row in rows]

    def pages(self, tenant_id: str, canonical_key: str | None = None) -> list[WikiPage]:
        """List immutable versions in deterministic topic/version order for relation compilation."""
        query = "SELECT payload FROM wiki_pages WHERE tenant_id=:tenant"
        values: dict[str, Any] = {"tenant": tenant_id}
        if canonical_key:
            query += " AND canonical_key=:key"
            values["key"] = canonical_key
        query += " ORDER BY canonical_key, version DESC"
        with self.engine.connect() as connection:
            rows = connection.execute(text(query), values).mappings().all()
        return [WikiPage.model_validate_json(row["payload"]) for row in rows]

    def page(self, tenant_id: str, page_id: str) -> WikiPage | None:
        """Read one page only inside its tenant ownership boundary."""
        with self.engine.connect() as connection:
            row = connection.execute(
                text("SELECT payload FROM wiki_pages WHERE tenant_id=:tenant AND page_id=:id"),
                {"tenant": tenant_id, "id": page_id},
            ).mappings().first()
        return WikiPage.model_validate_json(row["payload"]) if row else None

    def review(
        self,
        candidate: WikiCandidate,
        expected_status: str,
        build: Callable[[Any], tuple[list[WikiPage], list[OutboxEvent]]],
    ) -> tuple[list[WikiPage], list[OutboxEvent]]:
        """CAS the review state and publish pages/events in the same transaction."""
        with self.engine.begin() as connection:
            locked = connection.execute(
                text(
                    "SELECT status FROM wiki_candidates "
                    "WHERE tenant_id=:tenant AND candidate_id=:id"
                ),
                {"tenant": candidate.tenant_id, "id": candidate.candidate_id},
            ).mappings().first()
            if not locked or locked["status"] != expected_status:
                raise ValueError("candidate review state changed; reload and retry")
            pages, events = build(connection)
            changed = connection.execute(
                text("""UPDATE wiki_candidates SET status=:new_status,payload=:payload
                WHERE tenant_id=:tenant AND candidate_id=:id AND status=:expected"""),
                {"new_status": candidate.status.value, "payload": self._dump(candidate),
                 "tenant": candidate.tenant_id, "id": candidate.candidate_id,
                 "expected": expected_status},
            )
            if changed.rowcount != 1:
                raise ValueError("candidate review state changed; reload and retry")
            for page in pages:
                connection.execute(
                    text(
                        "INSERT INTO wiki_pages "
                        "VALUES(:tenant,:id,:key,:version,:status,:payload)"
                    ),
                    {"tenant": page.tenant_id, "id": page.page_id, "key": page.canonical_key,
                     "version": page.version, "status": page.status, "payload": self._dump(page)},
                )
            for event in events:
                connection.execute(
                    text("""INSERT INTO wiki_outbox(
                    event_id,tenant_id,event_type,attempts,status,next_attempt_at,lease_until,
                    lease_token,last_error,delivered_at,payload,created_at)
                    VALUES(:id,:tenant,:type,0,'pending',:next,NULL,'','',NULL,:payload,:created)"""),
                    {"id": event.event_id, "tenant": event.tenant_id, "type": event.event_type,
                     "next": event.next_attempt_at.isoformat(), "payload": self._dump(event),
                     "created": event.created_at.isoformat()},
                )
        return pages, events

    def pending_events(self, limit: int) -> list[OutboxEvent]:
        """Inspect deliverable events without leasing them; intended for health/tests only."""
        current = datetime.now(UTC).isoformat()
        with self.engine.connect() as connection:
            rows = connection.execute(
                text("""SELECT payload FROM wiki_outbox
                WHERE status IN ('pending','retry') AND next_attempt_at<=:now
                ORDER BY created_at LIMIT :limit"""),
                {"limit": limit, "now": current},
            ).mappings().all()
        return [OutboxEvent.model_validate_json(row["payload"]) for row in rows]

    def claim_events(self, limit: int, lease_seconds: int) -> list[OutboxEvent]:
        """Lease due events with CAS so parallel relay replicas cannot double-deliver a row."""
        current = datetime.now(UTC)
        token = uuid4().hex
        claimed: list[OutboxEvent] = []
        with self.engine.begin() as connection:
            connection.execute(
                text("""UPDATE wiki_outbox SET status='retry',lease_token='',lease_until=NULL
                WHERE status='processing' AND lease_until IS NOT NULL AND lease_until<:now"""),
                {"now": current.isoformat()},
            )
            rows = connection.execute(
                text("""SELECT event_id,payload FROM wiki_outbox
                WHERE status IN ('pending','retry') AND next_attempt_at<=:now
                ORDER BY created_at LIMIT :limit"""),
                {"now": current.isoformat(), "limit": limit},
            ).mappings().all()
            for row in rows:
                lease_until = current + timedelta(seconds=lease_seconds)
                changed = connection.execute(
                    text("""UPDATE wiki_outbox SET status='processing',lease_token=:token,
                    lease_until=:lease_until WHERE event_id=:id
                    AND status IN ('pending','retry')"""),
                    {"token": token, "lease_until": lease_until.isoformat(), "id": row["event_id"]},
                )
                if changed.rowcount == 1:
                    event = OutboxEvent.model_validate_json(row["payload"]).model_copy(
                        update={"status": "processing", "lease_token": token,
                                "lease_until": lease_until}
                    )
                    claimed.append(event)
        return claimed

    def mark_event(
        self,
        event: OutboxEvent,
        delivered: bool,
        *,
        error: str = "",
        max_attempts: int = 8,
        max_backoff_seconds: int = 300,
    ) -> None:
        """Finish a lease or schedule bounded retry; exhausted events move to inspectable DLQ."""
        current = datetime.now(UTC)
        attempts = event.attempts + 1
        if delivered:
            status = "delivered"
            next_attempt = event.next_attempt_at
            delivered_at = current
        else:
            status = "dlq" if attempts >= max_attempts else "retry"
            delay = min(max_backoff_seconds, 2 ** min(attempts, 20))
            next_attempt = current + timedelta(seconds=delay)
            delivered_at = None
        updated = event.model_copy(update={
            "attempts": attempts,
            "status": status,
            "next_attempt_at": next_attempt,
            "lease_until": None,
            "lease_token": "",
            "last_error": error[:4_000],
            "delivered_at": delivered_at,
        })
        with self.engine.begin() as connection:
            connection.execute(
                text("""UPDATE wiki_outbox SET attempts=:attempts,status=:status,
                next_attempt_at=:next_attempt,lease_until=NULL,lease_token='',last_error=:error,
                delivered_at=:delivered,payload=:payload
                WHERE event_id=:id AND status='processing' AND lease_token=:lease_token"""),
                {"attempts": updated.attempts, "status": status,
                 "next_attempt": next_attempt.isoformat(), "error": updated.last_error,
                 "delivered": delivered_at.isoformat() if delivered_at else None,
                 "payload": self._dump(updated), "id": event.event_id,
                 "lease_token": event.lease_token},
            )

    def dlq_events(self, limit: int = 100) -> list[OutboxEvent]:
        """Expose exhausted deliveries for operator-led diagnosis and controlled replay."""
        with self.engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT payload FROM wiki_outbox WHERE status='dlq' "
                    "ORDER BY created_at LIMIT :limit"
                ),
                {"limit": limit},
            ).mappings().all()
        return [OutboxEvent.model_validate_json(row["payload"]) for row in rows]

    def save_reindex_receipt(
        self, tenant_id: str, page_id: str, version: int, job_id: str
    ) -> None:
        """Persist the real RAG job correlation before a release gate can be requested."""
        if not job_id:
            raise ValueError("RAG ingestion response omitted job_id")
        with self.engine.begin() as connection:
            existing = connection.execute(
                text("""SELECT reindex_job_id FROM wiki_delivery_receipts
                WHERE tenant_id=:tenant AND page_id=:page AND version=:version"""),
                {"tenant": tenant_id, "page": page_id, "version": version},
            ).mappings().first()
            if existing and existing["reindex_job_id"] != job_id:
                raise ValueError("RAG ingestion job correlation changed for immutable Wiki version")
            if not existing:
                connection.execute(
                    text("""INSERT INTO wiki_delivery_receipts
                    VALUES(:tenant,:page,:version,:job,:created)"""),
                    {"tenant": tenant_id, "page": page_id, "version": version,
                     "job": job_id, "created": datetime.now(UTC).isoformat()},
                )

    def reindex_job_id(self, tenant_id: str, page_id: str, version: int) -> str:
        """Read the durable RAG receipt; absence means the release gate must remain blocked."""
        with self.engine.connect() as connection:
            row = connection.execute(
                text("""SELECT reindex_job_id FROM wiki_delivery_receipts
                WHERE tenant_id=:tenant AND page_id=:page AND version=:version"""),
                {"tenant": tenant_id, "page": page_id, "version": version},
            ).mappings().first()
        return str(row["reindex_job_id"]) if row else ""

    def close(self) -> None:
        """Dispose pooled database connections owned by this repository instance."""
        self.engine.dispose()
