"""Explicit, bounded DLQ replay command.

Replay is opt-in and resets the delivery-attempt header only for records chosen
by the operator.  It never drains a DLQ implicitly during consumer start-up.
"""

from __future__ import annotations

import argparse
import asyncio

from aiokafka import AIOKafkaConsumer, AIOKafkaProducer

from app.core.config import Settings
from app.infrastructure.kafka_consumer import _event


async def replay(settings: Settings, event_id: str, max_records: int) -> int:
    """Republish selected dead-letter events through normal idempotent ingestion."""
    consumer = AIOKafkaConsumer(
        settings.kafka_dlq_topic,
        bootstrap_servers=settings.kafka_bootstrap_servers,
        group_id=None,
        enable_auto_commit=False,
        auto_offset_reset="earliest",
    )
    producer = AIOKafkaProducer(
        bootstrap_servers=settings.kafka_bootstrap_servers,
        acks="all",
        enable_idempotence=True,
    )
    await consumer.start()
    await producer.start()
    replayed = 0
    try:
        idle = 0
        while replayed < max_records and idle < 3:
            batches = await consumer.getmany(timeout_ms=1000, max_records=100)
            idle = idle + 1 if not batches else 0
            for records in batches.values():
                for record in records:
                    event = _event(record.value)
                    if event_id and event.event_id != event_id:
                        continue
                    await producer.send_and_wait(
                        settings.kafka_governance_topic,
                        key=record.key,
                        value=record.value,
                        headers=[("x-replayed", b"true"), ("x-attempt", b"0")],
                    )
                    replayed += 1
                    if replayed >= max_records:
                        break
    finally:
        await consumer.stop()
        await producer.stop()
    return replayed


def main() -> None:
    """Perform main within the module ownership boundary."""
    parser = argparse.ArgumentParser(description="Explicitly replay Governance DLQ events")
    parser.add_argument("--event-id", default="")
    parser.add_argument("--max-records", type=int, default=1)
    parser.add_argument("--confirm", action="store_true")
    args = parser.parse_args()
    if not args.confirm:
        raise SystemExit("replay requires --confirm")
    settings = Settings()
    replayed = asyncio.run(replay(settings, args.event_id, args.max_records))
    print(f"replayed={replayed}")
