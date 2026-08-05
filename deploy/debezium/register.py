"""Idempotently register and verify the platform Debezium connector.

This process is deliberately separate from business services: database commits
remain local transactions, while Kafka Connect owns change capture and offset
durability.  Re-running the process updates the connector configuration rather
than creating an ambiguous second connector.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

CONNECT_URL = os.getenv("KAFKA_CONNECT_URL", "http://kafka-connect:8083").rstrip("/")
CONFIG_PATH = Path(os.getenv("DEBEZIUM_CONFIG_PATH", "/scripts/platform-outbox.json"))
MAX_WAIT_SECONDS = int(os.getenv("DEBEZIUM_REGISTER_TIMEOUT_SECONDS", "120"))
_PLACEHOLDER = re.compile(r"\$\{([A-Z][A-Z0-9_]*)\}")


def _request(
    path: str, method: str = "GET", body: object | None = None
) -> tuple[int, object]:
    """Call Kafka Connect without introducing a deployment-only dependency."""
    payload = None if body is None else json.dumps(body).encode()
    request = Request(
        f"{CONNECT_URL}{path}",
        data=payload,
        method=method,
        headers={"Content-Type": "application/json"} if payload else {},
    )
    try:
        with urlopen(request, timeout=10) as response:
            content = response.read().decode()
            return response.status, json.loads(content) if content else {}
    except HTTPError as exc:
        content = exc.read().decode()
        return exc.code, json.loads(content) if content else {}


def _resolve(value: object) -> object:
    """Resolve only explicit ${ENV} placeholders and fail closed when absent."""
    if isinstance(value, dict):
        return {key: _resolve(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_resolve(item) for item in value]
    if not isinstance(value, str):
        return value

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        resolved = os.getenv(name)
        if not resolved:
            raise RuntimeError(
                f"required connector environment variable {name} is not set"
            )
        return resolved

    return _PLACEHOLDER.sub(replace, value)


def _wait_for_connect() -> None:
    """Avoid a race with Connect's internal-topic initialization on cold start."""
    deadline = time.monotonic() + MAX_WAIT_SECONDS
    while time.monotonic() < deadline:
        try:
            status, _ = _request("/connectors")
            if status == 200:
                return
        except (URLError, TimeoutError, ValueError):
            pass
        time.sleep(2)
    raise RuntimeError(
        f"Kafka Connect did not become ready within {MAX_WAIT_SECONDS} seconds"
    )


def _connector_running(name: str) -> bool:
    """Require both connector and every task to run before declaring CDC ready."""
    status, body = _request(f"/connectors/{name}/status")
    if status != 200 or not isinstance(body, dict):
        return False
    connector = body.get("connector", {})
    tasks = body.get("tasks", [])
    return (
        connector.get("state") == "RUNNING"
        and bool(tasks)
        and all(task.get("state") == "RUNNING" for task in tasks)
    )


def main() -> None:
    """Upsert the singleton connector and make failed tasks fail the deployment."""
    raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    resolved = _resolve(raw)
    name = str(resolved["name"])
    config = resolved["config"]
    _wait_for_connect()
    status, _ = _request(f"/connectors/{name}")
    if status == 404:
        status, response = _request("/connectors", "POST", resolved)
    elif status == 200:
        status, response = _request(f"/connectors/{name}/config", "PUT", config)
    else:
        raise RuntimeError(f"cannot inspect connector {name}: HTTP {status}")
    if status not in {200, 201}:
        raise RuntimeError(
            f"cannot register connector {name}: HTTP {status}: {response}"
        )

    deadline = time.monotonic() + MAX_WAIT_SECONDS
    while time.monotonic() < deadline:
        if _connector_running(name):
            print(f"Debezium connector {name} is RUNNING", flush=True)
            return
        time.sleep(2)
    raise RuntimeError(f"Debezium connector {name} did not reach RUNNING state")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # deployment failure must be visible to the orchestrator
        print(f"Debezium registration failed: {exc}", file=sys.stderr, flush=True)
        raise
