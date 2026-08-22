"""Connector grant 的绑定字段和一次性消费语义测试。"""

from datetime import UTC, datetime, timedelta

import pytest

from app.infrastructure.repository import SqliteRepository


def test_connector_grant_is_bound_and_single_use(tmp_path) -> None:
    repo = SqliteRepository(tmp_path / "gateway.db")
    token = repo.issue_connector_grant(
        "tenant-a", "user-a", "connector-a", "run-a", "snapshot-a",
        "controlled_scan", "1.0.0", datetime.now(UTC) + timedelta(minutes=1),
    )

    assert repo.consume_connector_grant(
        "tenant-a", "user-a", "connector-a", token, "run-a", "snapshot-a",
        "controlled_scan", "1.0.0",
    ) is True
    assert repo.consume_connector_grant(
        "tenant-a", "user-a", "connector-a", token, "run-a", "snapshot-a",
        "controlled_scan", "1.0.0",
    ) is False


def test_connector_grant_rejects_wrong_snapshot(tmp_path) -> None:
    repo = SqliteRepository(tmp_path / "gateway.db")
    token = repo.issue_connector_grant(
        "tenant-a", "user-a", "connector-a", "run-a", "snapshot-a",
        "controlled_scan", "1.0.0", datetime.now(UTC) + timedelta(minutes=1),
    )

    assert repo.consume_connector_grant(
        "tenant-a", "user-a", "connector-a", token, "run-a", "snapshot-b",
        "controlled_scan", "1.0.0",
    ) is False


def test_connector_result_receipt_is_idempotent_and_rejects_conflict(tmp_path) -> None:
    repo = SqliteRepository(tmp_path / "gateway.db")
    repo.save_connector_result_receipt("tenant-a", "connector-a", "task-a", "a" * 64, "inv-a")

    assert repo.connector_result_receipt("tenant-a", "connector-a", "task-a", "a" * 64) == "inv-a"
    with pytest.raises(ValueError, match="conflicts"):
        repo.connector_result_receipt("tenant-a", "connector-a", "task-a", "b" * 64)
