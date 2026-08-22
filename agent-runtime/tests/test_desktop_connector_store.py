"""Desktop Connector 配对关系的原子状态流转测试。"""

from datetime import UTC, datetime, timedelta

from agent_runtime_service.runtime.integration import RuntimeStoreOperations


def test_connector_pairing_is_one_time_and_revocation_is_terminal(tmp_path) -> None:
    """正确配对只能消费一次，撤销后不能再次确认。"""

    store = RuntimeStoreOperations(tmp_path / "runtime.db")
    expires_at = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
    connector_id = store.create_connector(
        "tenant-a", "user-a", "desktop", ["workspace:read"], "hash-a", expires_at
    )

    assert store.confirm_connector("tenant-a", "user-a", connector_id, "wrong") is False
    assert store.confirm_connector("tenant-a", "user-a", connector_id, "hash-a") is True
    assert store.confirm_connector("tenant-a", "user-a", connector_id, "hash-a") is False
    assert store.get_connector("tenant-a", "user-a", connector_id)["status"] == "CONNECTED"
    assert store.heartbeat_connector("tenant-a", "user-a", connector_id) is True
    assert store.get_connector("tenant-a", "user-a", connector_id)["last_seen_at"]

    assert store.reconcile_stale_connectors(0) == 1
    assert store.get_connector("tenant-a", "user-a", connector_id)["status"] == "DISCONNECTED"
    assert store.heartbeat_connector("tenant-a", "user-a", connector_id) is False

    assert store.revoke_connector("tenant-a", "user-a", connector_id) is True
    assert store.get_connector("tenant-a", "user-a", connector_id)["status"] == "REVOKED"
    assert store.confirm_connector("tenant-a", "user-a", connector_id, "hash-a") is False
    assert store.heartbeat_connector("tenant-a", "user-a", connector_id) is False


def test_expired_connector_cannot_be_confirmed(tmp_path) -> None:
    """过期配对码即使正确也必须 fail-closed。"""

    store = RuntimeStoreOperations(tmp_path / "runtime.db")
    expires_at = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    connector_id = store.create_connector("tenant-a", "user-a", "desktop", [], "hash-a", expires_at)

    assert store.confirm_connector("tenant-a", "user-a", connector_id, "hash-a") is False
    assert store.get_connector("tenant-a", "user-a", connector_id)["status"] == "PENDING"


def test_connector_task_can_only_be_claimed_once_within_lease(tmp_path) -> None:
    store = RuntimeStoreOperations(tmp_path / "runtime.db")
    connector_id = store.create_connector(
        "tenant-a", "user-a", "desktop", ["controlled_scan"], "hash-a",
        (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
    )
    assert store.confirm_connector("tenant-a", "user-a", connector_id, "hash-a")
    task_id = store.create_connector_task(
        "tenant-a", "user-a", connector_id, "run-a", "snapshot-a", "controlled_scan",
        "1.0.0", {"query": "TODO"}, (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
    )

    claimed = store.claim_connector_task("tenant-a", "user-a", connector_id)
    assert claimed and claimed["task_id"] == task_id
    assert claimed["arguments"] == {"query": "TODO"}
    assert store.claim_connector_task("tenant-a", "user-a", connector_id) is None
    assert store.complete_connector_task(
        "tenant-a", "user-a", connector_id, task_id, {"findings": []}, "digest"
    ) is True
    assert store.complete_connector_task(
        "tenant-a", "user-a", connector_id, task_id, {"findings": []}, "digest"
    ) is False


def test_connector_artifact_outbox_is_tenant_scoped_leased_and_delivered(tmp_path) -> None:
    """Relay 只能领取指定租户记录，且旧 lease 不能确认另一个 Worker 的交付。"""

    store = RuntimeStoreOperations(tmp_path / "runtime.db")
    for tenant in ("tenant-a", "tenant-b"):
        store.enqueue_connector_artifact(
            tenant, "user-a", f"task-{tenant}", f"root-{tenant}", {"answer": tenant}, "down"
        )

    claimed = store.claim_connector_artifacts(tenant_id="tenant-a", limit=10, lease_seconds=60)
    assert len(claimed) == 1
    assert claimed[0]["tenant_id"] == "tenant-a"
    assert store.claim_connector_artifacts(tenant_id="tenant-a", limit=10) == []
    assert (
        store.mark_connector_artifact_delivered(
            claimed[0]["outbox_id"], "wrong-lease", "artifact-a"
        )
        is False
    )
    assert store.mark_connector_artifact_delivered(
        claimed[0]["outbox_id"], claimed[0]["lease_token"], "artifact-a"
    )


def test_connector_artifact_outbox_retries_then_enters_and_leaves_dlq(tmp_path) -> None:
    """达到失败阈值后形成可查询死信，并且只能由同租户显式重排。"""

    store = RuntimeStoreOperations(tmp_path / "runtime.db")
    store.enqueue_connector_artifact(
        "tenant-a", "user-a", "task-a", "root-a", {"answer": "redacted"}, "down"
    )
    first = store.claim_connector_artifacts(tenant_id="tenant-a")[0]
    assert store.fail_connector_artifact_delivery(
        first["outbox_id"], first["lease_token"], "timeout", max_attempts=1
    ) == "DEAD_LETTER"
    dead_letters = store.list_connector_artifact_dead_letters("tenant-a")
    assert dead_letters[0]["outbox_id"] == first["outbox_id"]
    assert "content_json" not in dead_letters[0]
    assert not store.requeue_connector_artifact_dead_letter("tenant-b", first["outbox_id"])
    assert store.requeue_connector_artifact_dead_letter("tenant-a", first["outbox_id"])
    assert store.list_connector_artifact_dead_letters("tenant-a") == []
