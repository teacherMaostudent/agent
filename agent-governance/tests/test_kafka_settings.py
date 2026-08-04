from app.core.config import Settings


def test_kafka_poll_and_retry_controls_are_configurable() -> None:
    settings = Settings(
        kafka_max_poll_records=500,
        kafka_retry_backoff_max_seconds=60,
    )
    assert settings.kafka_max_poll_records == 500
    assert settings.kafka_retry_backoff_max_seconds == 60
