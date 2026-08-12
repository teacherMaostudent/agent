"""Fail fast when production configuration loses a required platform closure.

The check is intentionally static and dependency-free so it runs in CI before
credentials or infrastructure are available.  ``--connect-url`` adds a live
Kafka Connect task-status check during deployment verification.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]


def _require_text(path: Path, required: list[str], failures: list[str]) -> None:
    """Assert that a production guard is present without parsing every service."""
    content = path.read_text(encoding="utf-8")
    for token in required:
        if token not in content:
            failures.append(f"{path.relative_to(ROOT)} is missing {token!r}")


def _check_static(failures: list[str]) -> None:
    """Check the critical event, identity and durable-state deployment edges."""
    connector = json.loads((ROOT / "deploy/debezium/platform-outbox.json").read_text())
    config = connector.get("config", {})
    expected = {
        "connector.class": "io.debezium.connector.postgresql.PostgresConnector",
        "snapshot.mode": "no_data",
        "slot.drop.on.stop": "false",
        "publication.name": "agent_platform_outbox_publication",
        "transforms.unwrap.type": "io.debezium.transforms.ExtractNewRecordState",
        "transforms.route.replacement": "agent.governance.events.v1",
    }
    for key, value in expected.items():
        if config.get(key) != value:
            failures.append(f"Debezium config {key!r} must be {value!r}")
    tables = str(config.get("table.include.list", ""))
    for table in (
        "control_plane.outbox_events",
        "runtime_platform.runtime_outbox",
        "tool_gateway.event_outbox",
    ):
        if table not in tables:
            failures.append(f"Debezium table filter is missing {table}")

    _require_text(
        ROOT / "compose.production.yaml",
        [
            "debezium-register:",
            "RAG_GOVERNANCE_DELIVERY_MODE: cdc",
            "RUNTIME_GOVERNANCE_DELIVERY_MODE: cdc",
            "TOOL_GATEWAY_GOVERNANCE_DELIVERY_MODE: cdc",
            'GOVERNANCE_CDC_REQUIRED: "true"',
            "GOVERNANCE_JUDGE_PRIMARY_MODEL_REVISION",
            "GOVERNANCE_JUDGE_SECONDARY_MODEL_REVISION",
            "GOVERNANCE_JUDGE_ARBITRATOR_MODEL_REVISION",
            "condition: service_completed_successfully",
            "wal_level=logical",
        ],
        failures,
    )
    _require_text(
        ROOT / "rag-agent-service/app/core/config.py",
        ["RAG_GOVERNANCE_DELIVERY_MODE must be cdc"],
        failures,
    )
    _require_text(
        ROOT / "tool-gateway/app/core/config.py",
        ["TOOL_GATEWAY_GOVERNANCE_DELIVERY_MODE must be cdc"],
        failures,
    )
    _require_text(
        ROOT / "deploy/debezium/register.py",
        ["_connector_running", "PUT", "did not reach RUNNING state"],
        failures,
    )


def _check_connector(connect_url: str, failures: list[str]) -> None:
    """Confirm the deployed connector and every task are actually running."""
    url = f"{connect_url.rstrip('/')}/connectors/agent-platform-outbox/status"
    try:
        with urlopen(url, timeout=10) as response:
            status = json.loads(response.read().decode())
    except (HTTPError, URLError, TimeoutError, ValueError) as exc:
        failures.append(f"cannot read Kafka Connect status: {exc}")
        return
    if status.get("connector", {}).get("state") != "RUNNING":
        failures.append(
            f"connector state is {status.get('connector', {}).get('state')!r}"
        )
    tasks = status.get("tasks", [])
    if not tasks or any(task.get("state") != "RUNNING" for task in tasks):
        failures.append(f"connector tasks are not all RUNNING: {tasks!r}")


def main() -> int:
    """Run static checks locally and optional live checks after Compose/Kubernetes rollout."""
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--connect-url", help="Kafka Connect REST endpoint for live validation"
    )
    args = parser.parse_args()
    failures: list[str] = []
    _check_static(failures)
    if args.connect_url:
        _check_connector(args.connect_url, failures)
    if failures:
        print(
            "Production-readiness check failed:",
            *[f"- {item}" for item in failures],
            sep="\n",
        )
        return 1
    print("Production-readiness check passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
