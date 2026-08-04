"""Black-box integration gate for `compose.platform.yaml`.

It proves the critical cross-service chain: Control Plane release resolution ->
Runtime durable Run -> Context/RAG -> Governance outbox, plus Tool Gateway's
execution-context propagation and its independent Governance outbox.
"""

from __future__ import annotations

import sys
import time
from typing import Any

import httpx

BASE = {
    "control": "http://localhost:8082",
    "governance": "http://localhost:8081",
    "runtime": "http://localhost:8001",
    "tool": "http://localhost:8090",
}
TENANT = "e2e-tenant"


def request(client: httpx.Client, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
    response = client.request(method, url, **kwargs)
    response.raise_for_status()
    return response.json()


def spec() -> dict[str, Any]:
    return {
        "display_name": "E2E GMP Review Agent",
        "description": "Controlled integration fixture",
        "graph": {
            "graph_id": "e2e-graph", "entrypoint": "answer", "terminal_nodes": ["answer"],
            "nodes": [{"node_id": "answer", "kind": "answer", "config": {}}], "edges": [],
        },
        "prompt": {"prompt_id": "e2e-prompt", "system_template": "Answer from evidence.", "variables": []},
        "tools": [], "knowledge": [],
        "model_policy": {"policy_id": "e2e-policy", "default_route": "chat", "routes": [
            {"route_name": "chat", "capability": "chat", "models": ["deepseek-v4-flash"]}
        ]},
        "runtime_limits": {"max_steps": 4, "max_llm_calls": 2, "max_tool_calls": 2,
                           "max_retrieval_rounds": 2, "max_execution_seconds": 30, "max_cost_usd": 1.0},
        "labels": {"fixture": "platform-e2e"},
    }


def wait_for(client: httpx.Client, url: str) -> None:
    for _ in range(30):
        try:
            if client.get(url).is_success:
                return
        except httpx.HTTPError:
            pass
        time.sleep(1)
    raise RuntimeError(f"not ready: {url}")


def main() -> int:
    with httpx.Client(timeout=15) as client:
        for target in (f"{BASE['control']}/health/ready", f"{BASE['governance']}/health/ready",
                       f"{BASE['runtime']}/api/v1/health/ready", f"{BASE['tool']}/api/v1/health/ready"):
            wait_for(client, target)

        manage = {
            "X-Tenant-Id": TENANT,
            "X-User-Id": "e2e-admin",
            "X-Roles": "agent-admin",
            "X-Control-Plane-Admin-Key": "local-control-plane-admin-key",
        }
        agent = f"gmp-e2e-{int(time.time())}"
        created = request(client, "POST", f"{BASE['control']}/v1/agents", headers=manage,
                          json={"agent_id": agent, "spec": spec()})
        version = request(client, "POST", f"{BASE['control']}/v1/agents/{agent}/versions", headers=manage,
                          json={"semantic_version": "1.0.0", "change_summary": "E2E fixture"})
        request(client, "POST", f"{BASE['control']}/v1/agents/{agent}/releases", headers=manage, json={
            "version_id": version["version_id"], "environment": "local", "rollout_percentage": 100,
            "reason": "E2E fixture",
        })
        assert created["agent_id"] == agent

        trace_id = "trace-platform-e2e"
        run = request(client, "POST", f"{BASE['runtime']}/api/v1/agent/run", headers={
            "X-Tenant-Id": TENANT, "X-User-Id": "e2e-user", "X-Permissions": "rag:read",
            "X-Trace-Id": trace_id, "X-Rag-Agent-Key": "local-rag-service-key",
        }, json={"agent_id": agent, "environment": "local", "task": "Find evidence for the E2E check."})
        persisted = request(client, "GET", f"{BASE['runtime']}/api/v1/agent/runs/{run['run_id']}", headers={
            "X-Tenant-Id": TENANT, "X-Rag-Agent-Key": "local-rag-service-key",
        })
        assert persisted["status"] == "COMPLETED"
        assert persisted["context"]["snapshot_id"] == version["version_id"]

        request(client, "POST", f"{BASE['tool']}/api/v1/tools/create_ingestion_job/invoke", headers={
            "X-Tool-Gateway-Key": "local-tool-gateway-key", "X-Tenant-Id": TENANT,
            "X-User-Id": "e2e-user", "X-Permissions": "ingestion:write", "X-Request-Id": "e2e-tool-request",
            "X-Idempotency-Key": "e2e-tool-idempotency-0001", "X-Trace-Id": trace_id,
            "X-Run-Id": run["run_id"], "X-Agent-Id": agent, "X-Agent-Version": "1.0.0",
            "X-Snapshot-Id": version["version_id"],
        }, json={"arguments": {"job_type": "REINDEX"}})

        audit = request(client, "GET", f"{BASE['governance']}/v1/governance/audit-events", headers={
            "X-Tenant-Id": TENANT, "X-User-Id": "e2e-auditor", "X-Roles": "governance-auditor",
            "X-Governance-Auditor-Key": "local-governance-auditor-key",
        })
        types = {item["event_type"] for item in audit["items"]}
        assert {"agent.run.completed", "tool.execution.completed"}.issubset(types), types
    print("platform E2E passed: release resolution, persisted run, tool propagation, governance events")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (AssertionError, httpx.HTTPError, RuntimeError) as error:
        print(f"platform E2E failed: {error}", file=sys.stderr)
        raise SystemExit(1)
