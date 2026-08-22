import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.application.worm_export_service import WormExportService
from app.core.config import Settings
from app.domain.models import AuditEvent
from app.infrastructure import worm_exporter
from app.infrastructure.sqlite_repository import SqliteRepository


@pytest.mark.asyncio
async def test_worm_export_job_is_deduplicated_leased_and_completed(tmp_path: Path) -> None:
    """活跃导出只能有一个，Worker 必须持有 CAS 租约才能提交对象证明。"""
    project = Path(__file__).resolve().parents[1]
    repository = SqliteRepository(tmp_path / "governance.db", project / "db" / "schema.sql")
    await repository.initialize()
    service = WormExportService(repository, Settings())

    created = await service.create("tenant-a", "auditor-a")
    duplicate = await service.create("tenant-a", "auditor-b")
    assert duplicate["job_id"] == created["job_id"]

    claimed = await service.claim("worker-a")
    assert claimed and claimed["status"] == "RUNNING"
    assert await service.claim("worker-b") is None
    await service.complete(
        claimed,
        {
            "object_key": "audit/object.json",
            "sha256": "a" * 64,
            "merkle_root": "b" * 64,
        },
    )
    completed = await service.get("tenant-a", created["job_id"])
    assert completed and completed["status"] == "COMPLETED"
    assert completed["result"]["object_key"] == "audit/object.json"


@pytest.mark.asyncio
async def test_worm_export_exhaustion_enters_dlq_and_requires_explicit_requeue(
    tmp_path: Path,
) -> None:
    """失败达到预算后不会无限重试，只有显式审计动作可重新排队。"""
    project = Path(__file__).resolve().parents[1]
    repository = SqliteRepository(tmp_path / "governance.db", project / "db" / "schema.sql")
    await repository.initialize()
    service = WormExportService(repository, Settings(worm_export_max_attempts=1))
    created = await service.create("tenant-a", "auditor-a")
    claimed = await service.claim("worker-a")
    assert claimed
    await service.fail(claimed, RuntimeError("kms unavailable"))
    failed = await service.get("tenant-a", created["job_id"])
    assert failed and failed["status"] == "DLQ"
    requeued = await service.requeue("tenant-a", created["job_id"], "auditor-b")
    assert requeued and requeued["status"] == "QUEUED"


@pytest.mark.asyncio
async def test_worm_export_envelope_contains_verifiable_streamed_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stored envelope must bind the exact export body, signature and Merkle root."""
    project = Path(__file__).resolve().parents[1]
    repository = SqliteRepository(tmp_path / "governance.db", project / "db" / "schema.sql")
    await repository.initialize()
    event = AuditEvent(
        event_id="event-1",
        source_service="agent-runtime",
        event_type="runtime.demo.completed",
        trace_id="trace-1",
        tenant_id="tenant-a",
        occurred_at=datetime.now(UTC),
        payload={"run_id": "run-1"},
        sequence=0,
        received_at=datetime.now(UTC),
    )
    assert await repository.ingest(event, []) is True
    captured: dict[str, bytes] = {}

    class Storage:
        """Capture the upload while retaining the production streaming call shape."""

        def __init__(self, **_kwargs):
            pass

        def put_stream(self, _namespace, key, stream, **_kwargs):
            captured["body"] = stream.read()
            return key, hashlib.sha256(captured["body"]).hexdigest()

    monkeypatch.setattr(worm_exporter, "S3ObjectStorage", Storage)
    result = await worm_exporter.export_tenant(
        Settings(
            worm_bucket="worm",
            worm_signing_mode="hmac-local",
            worm_local_signing_key="test-only-signing-key",
            worm_retention_days=30,
        ),
        "tenant-a",
        repository,
    )

    envelope = json.loads(captured["body"])
    canonical_export = json.dumps(
        envelope["export"], ensure_ascii=False, separators=(",", ":")
    ).encode()
    assert envelope["sha256"] == hashlib.sha256(canonical_export).hexdigest()
    assert envelope["export"]["event_count"] == 1
    assert envelope["export"]["events"][0]["event_id"] == "event-1"
    assert result["merkle_root"] == envelope["export"]["merkle_root"]
