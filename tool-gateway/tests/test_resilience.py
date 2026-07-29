from __future__ import annotations

import asyncio

from app.domain.errors import ToolUpstreamError

from .conftest import tool_spec


def test_retry_then_success(gateway_factory, trusted_headers) -> None:
    calls = {"count": 0}

    def flaky(args, context):
        calls["count"] += 1
        if calls["count"] == 1:
            raise ToolUpstreamError("temporary", retryable=True)
        return {"ok": True}

    client = gateway_factory([(tool_spec("retry_tool", retry_attempts=2), flaky)])

    response = client.post(
        "/api/v1/tools/retry_tool/invoke",
        headers=trusted_headers,
        json={"arguments": {"value": "ok"}},
    )

    assert response.status_code == 200
    assert response.json()["attempt_count"] == 2
    assert calls["count"] == 2


def test_timeout_is_bounded(gateway_factory, trusted_headers) -> None:
    async def slow(args, context):
        await asyncio.sleep(0.1)
        return {"ok": True}

    client = gateway_factory([(tool_spec("slow_tool", timeout_seconds=0.01), slow)])

    response = client.post(
        "/api/v1/tools/slow_tool/invoke",
        headers=trusted_headers,
        json={"arguments": {"value": "ok"}},
    )

    assert response.status_code == 504
    assert response.json()["error"]["code"] == "tool_timeout"


def test_circuit_opens_after_failure_threshold(gateway_factory, trusted_headers) -> None:
    calls = {"count": 0}

    def failing(args, context):
        calls["count"] += 1
        raise ToolUpstreamError("down", retryable=True)

    client = gateway_factory(
        [
            (
                tool_spec(
                    "circuit_tool",
                    retry_attempts=1,
                    breaker_failure_threshold=2,
                ),
                failing,
            )
        ]
    )
    payload = {"arguments": {"value": "ok"}}

    first = client.post(
        "/api/v1/tools/circuit_tool/invoke",
        headers=trusted_headers,
        json=payload,
    )
    second = client.post(
        "/api/v1/tools/circuit_tool/invoke",
        headers=trusted_headers,
        json=payload,
    )
    third = client.post(
        "/api/v1/tools/circuit_tool/invoke",
        headers=trusted_headers,
        json=payload,
    )

    assert first.status_code == 502
    assert second.status_code == 502
    assert third.status_code == 503
    assert third.json()["error"]["code"] == "tool_circuit_open"
    assert calls["count"] == 2
