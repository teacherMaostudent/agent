from __future__ import annotations

import json

from app.infrastructure.kafka_consumer import _attempts, _event
from app.infrastructure.worm_exporter import _merkle_root


def test_debezium_outbox_envelope_becomes_canonical_event() -> None:
    value = json.dumps(
        {
            "payload": {
                "source": {"schema": "control_plane"},
                "after": {
                    "event_id": "event-1",
                    "event_type": "agent.release.published",
                    "trace_id": "trace-1",
                    "tenant_id": "tenant-a",
                    "occurred_at": "2026-08-01T00:00:00Z",
                    "payload_json": json.dumps({"release_id": "release-1"}),
                },
            }
        }
    ).encode()

    event = _event(value)

    assert event.event_id == "event-1"
    assert event.source_service == "control_plane"
    assert event.payload == {"release_id": "release-1"}


def test_retry_attempt_header_is_defensive() -> None:
    assert _attempts([("x-attempt", b"3")]) == 3
    assert _attempts([("x-attempt", b"not-a-number")]) == 0
    assert _attempts(None) == 0


def test_merkle_root_is_deterministic_and_handles_odd_leaf_count() -> None:
    leaves = ["00" * 32, "11" * 32, "22" * 32]
    assert _merkle_root(leaves) == _merkle_root(list(leaves))
    assert _merkle_root([]) == ("e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855")
