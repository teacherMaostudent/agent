"""At-least-once Governance event ingestion with bounded retry and a DLQ.

Offsets are committed only after each batch has either been persisted or
durably forwarded to retry/DLQ.  Consumers must therefore tolerate duplicate
events; the Governance repository provides the deduplication boundary.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import signal
from typing import Any

from aiokafka import AIOKafkaConsumer, AIOKafkaProducer

from app.container import AppContainer
from app.core.config import Settings
from app.domain.models import GovernanceEvent


def _event(value: bytes) -> GovernanceEvent:
    """Normalize direct, outbox and Debezium envelopes into one event contract."""
    document: Any = json.loads(value)
    if isinstance(document, dict) and isinstance(document.get("payload"), dict):
        document = document["payload"]
    if isinstance(document, dict) and isinstance(document.get("after"), dict):
        after = document["after"]
        payload = after.get("payload_json")
        if isinstance(payload, str):
            payload = json.loads(payload)
        if isinstance(payload, dict) and "event_id" in payload:
            document = payload
        else:
            source = document.get("source", {})
            document = {
                "event_id": after["event_id"],
                "source_service": str(source.get("name") or source.get("schema") or "outbox"),
                "event_type": after.get("event_type", "platform.outbox.event"),
                "trace_id": after.get("trace_id", after["event_id"]),
                "tenant_id": after["tenant_id"],
                "occurred_at": after.get("occurred_at"),
                "payload": payload or {},
            }
    elif isinstance(document, dict) and "payload" in document:
        payload = document["payload"]
        document = json.loads(payload) if isinstance(payload, str) else payload
    return GovernanceEvent.model_validate(document)


async def consume(settings: Settings) -> None:
    """Consume with idempotent Kafka writes before committing source offsets."""
    container = AppContainer(settings)
    await container.start()
    consumer = AIOKafkaConsumer(
        settings.kafka_governance_topic,
        settings.kafka_retry_topic,
        bootstrap_servers=settings.kafka_bootstrap_servers,
        group_id=settings.kafka_consumer_group,
        enable_auto_commit=False,
        auto_offset_reset="earliest",
        isolation_level="read_committed",
    )
    producer = AIOKafkaProducer(
        bootstrap_servers=settings.kafka_bootstrap_servers,
        acks="all",
        enable_idempotence=True,
    )
    await consumer.start()
    await producer.start()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for event in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(event, stop.set)
    try:
        while not stop.is_set():
            batches = await consumer.getmany(
                timeout_ms=1000,
                max_records=settings.kafka_max_poll_records,
            )
            for records in batches.values():
                for record in records:
                    try:
                        await container.service.ingest(_event(record.value))
                    except Exception as exc:
                        # Do not abandon an offset silently: the failed record
                        # is first made visible to a retry topic or DLQ, then
                        # the source batch can be committed safely.
                        attempts = _attempts(record.headers) + 1
                        topic = (
                            settings.kafka_retry_topic
                            if attempts < settings.kafka_max_attempts
                            else settings.kafka_dlq_topic
                        )
                        if topic == settings.kafka_retry_topic:
                            await asyncio.sleep(
                                min(settings.kafka_retry_backoff_max_seconds, 2**attempts)
                            )
                        await producer.send_and_wait(
                            topic,
                            key=record.key,
                            value=record.value,
                            headers=[
                                ("x-attempt", str(attempts).encode()),
                                ("x-error", f"{type(exc).__name__}: {exc}"[:500].encode()),
                                ("x-original-topic", record.topic.encode()),
                            ],
                        )
            if batches:
                await consumer.commit()
    finally:
        await consumer.stop()
        await producer.stop()


def main() -> None:
    """Perform main within the module ownership boundary."""
    settings = Settings()
    if not settings.kafka_bootstrap_servers:
        raise RuntimeError("GOVERNANCE_KAFKA_BOOTSTRAP_SERVERS is required")
    asyncio.run(consume(settings))

def _attempts(headers: list[tuple[str, bytes]] | None) -> int:
    """Internal helper for module; preserve its caller-facing invariant."""
    for name, value in headers or []:
        if name == "x-attempt":
            try:
                return int(value.decode())
            except ValueError:
                return 0
    return 0
