"""Black-box integration gate for `compose.platform.yaml`.

It proves the critical cross-service chain: Control Plane release resolution ->
Runtime durable Run -> Context/RAG -> Governance outbox, plus Tool Gateway's
execution-context propagation and its independent Governance outbox.
"""

from __future__ import annotations

import sys
import time
import uuid
from typing import Any

import httpx

BASE = {
    "control": "http://localhost:9002",
    "governance": "http://localhost:9001",
    "runtime": "http://localhost:8001",
    "tool": "http://localhost:9090",
}
TENANT = "e2e-tenant"
DESKTOP_TENANT = "demo"
DESKTOP_AGENT = "general-agent"


def request(client: httpx.Client, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
    response = client.request(method, url, **kwargs)
    response.raise_for_status()
    return response.json()


def spec() -> dict[str, Any]:
    return {
        "display_name": "E2E General Agent",
        "description": "Controlled integration fixture",
        "graph": {
            "graph_id": "e2e-graph", "entrypoint": "decide", "terminal_nodes": ["answer"],
            "nodes": [
                {"node_id": "decide", "kind": "decision", "config": {}},
                {"node_id": "retrieve", "kind": "retrieval", "config": {}},
                {"node_id": "answer", "kind": "answer", "config": {}},
            ],
            "edges": [
                # 真实模型可以先检索，也可以在无知识绑定时直接回答；两条转移均须在
                # Release 中显式声明，不能依赖离线引擎总会选择 RETRIEVE 的偶然行为。
                {
                    "from_node": "decide",
                    "to_node": "retrieve",
                    "condition": 'decision.action == "RETRIEVE"',
                },
                {
                    "from_node": "decide",
                    "to_node": "answer",
                    "condition": 'decision.action == "ANSWER"',
                },
                {"from_node": "retrieve", "to_node": "decide"},
            ],
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


def desktop_spec() -> dict[str, Any]:
    """生成本地桌面演示的受控 Release，而不污染纯跨服务 E2E fixture。"""
    value = spec()
    value.update(
        {
            "display_name": "Desktop General Agent",
            "description": "Local desktop baseline with an explicitly published read-only scan tool.",
            # 发布 Graph 必须显式允许 decision -> tool -> decision -> answer。只绑定工具
            # 但遗漏该迁移会被 Runtime fail-closed，不能让工具权限绕开 Workflow Policy。
            "graph": {
                "graph_id": "desktop-controlled-graph",
                "entrypoint": "decide",
                "terminal_nodes": ["answer"],
                "nodes": [
                    {"node_id": "decide", "kind": "decision", "config": {}},
                    {"node_id": "retrieve", "kind": "retrieval", "config": {}},
                    {"node_id": "tool", "kind": "tool", "config": {}},
                    {"node_id": "answer", "kind": "answer", "config": {}},
                ],
                "edges": [
                    {
                        "from_node": "decide",
                        "to_node": "retrieve",
                        "condition": 'decision.action == "RETRIEVE"',
                    },
                    {
                        "from_node": "decide",
                        "to_node": "tool",
                        "condition": 'decision.action == "TOOL"',
                    },
                    {
                        "from_node": "decide",
                        "to_node": "answer",
                        "condition": 'decision.action == "ANSWER"',
                    },
                    {"from_node": "retrieve", "to_node": "decide"},
                    {"from_node": "tool", "to_node": "decide"},
                ],
            },
            "tools": [
                {
                    "tool_name": "controlled_scan",
                    "version": "1.0.0",
                    # 工具目录会在 Gateway 端再次冻结风险、权限和幂等属性；这里的声明
                    # 仅表达 Release 所允许的最小调用范围。
                    "risk": "read_only",
                    "approval_required": False,
                    "idempotent": True,
                    "required_permissions": ["file:scan"],
                }
            ],
            # 外部模型单次调用在网络抖动时可超过 30 秒。桌面基线的上限必须覆盖一次
            # 规划、一次工具决策和一次最终回答；仍由成本、调用次数及步骤上限共同约束。
            "runtime_limits": {
                "max_steps": 8,
                "max_llm_calls": 4,
                "max_tool_calls": 3,
                "max_retrieval_rounds": 3,
                "max_execution_seconds": 180,
                "max_cost_usd": 1.0,
            },
            "labels": {"desktop_baseline": "v6", "fixture": "desktop-local"},
        }
    )
    return value


def wait_for(client: httpx.Client, url: str) -> None:
    for _ in range(30):
        try:
            if client.get(url).is_success:
                return
        except httpx.HTTPError:
            pass
        time.sleep(1)
    raise RuntimeError(f"not ready: {url}")


def management_headers(tenant_id: str) -> dict[str, str]:
    """构造本地联调管理身份；生产部署不得复用这些示例密钥。"""
    return {
        "X-Tenant-Id": tenant_id,
        "X-User-Id": "local-bootstrap",
        "X-Roles": "agent-admin",
        "X-Control-Plane-Admin-Key": "local-control-plane-admin-key",
    }


def ensure_desktop_release(client: httpx.Client) -> None:
    """幂等准备桌面端默认 Agent，并将旧的无工具 Release 迁移到桌面基线。"""
    resolve_url = f"{BASE['control']}/v1/runtime/agents/{DESKTOP_AGENT}/resolve"
    runtime_headers = {
        "X-Tenant-Id": DESKTOP_TENANT,
        "X-User-Id": "agent-runtime",
        "X-Runtime-Key": "local-control-plane-key",
    }
    manage = management_headers(DESKTOP_TENANT)
    agent_url = f"{BASE['control']}/v1/agents/{DESKTOP_AGENT}"
    agent_response = client.get(agent_url, headers=manage)
    if agent_response.status_code == 404:
        request(
            client,
            "POST",
            f"{BASE['control']}/v1/agents",
            headers=manage,
            json={"agent_id": DESKTOP_AGENT, "spec": desktop_spec()},
        )
    else:
        agent_response.raise_for_status()
        agent = agent_response.json()
        if agent.get("draft", {}).get("labels", {}).get("desktop_baseline") != "v6":
            request(
                client,
                "PUT",
                f"{agent_url}/draft",
                headers=manage,
                json={
                    "expected_revision": agent["revision"],
                    "spec": desktop_spec(),
                },
            )

    versions = request(
        client,
        "GET",
        f"{BASE['control']}/v1/agents/{DESKTOP_AGENT}/versions",
        headers=manage,
    )
    version = next(
        (item for item in versions if item["semantic_version"] == "1.5.0"),
        None,
    )
    if version is None:
        version = request(
            client,
            "POST",
            f"{BASE['control']}/v1/agents/{DESKTOP_AGENT}/versions",
            headers=manage,
            json={
                "semantic_version": "1.5.0",
                "change_summary": "Separate hosted-model timeout from the end-to-end desktop deadline",
            },
        )

    releases = request(
        client,
        "GET",
        f"{BASE['control']}/v1/agents/{DESKTOP_AGENT}/releases?environment=local",
        headers=manage,
    )
    if not any(
        item["status"] == "active" and item["version_id"] == version["version_id"]
        for item in releases
    ):
        request(
            client,
            "POST",
            f"{BASE['control']}/v1/agents/{DESKTOP_AGENT}/releases",
            headers=manage,
            json={
                "version_id": version["version_id"],
                "environment": "local",
                "rollout_percentage": 100,
                "reason": "Local desktop bootstrap",
            },
        )

    verified = client.get(
        resolve_url,
        params={"environment": "local", "session_id": "desktop-bootstrap"},
        headers=runtime_headers,
    )
    verified.raise_for_status()


def main() -> int:
    # 真实模型模式一次 Run 可以包含多次上游调用；15 秒只适合离线确定性路径，
    # 会把仍在执行的正常 Run 错判为联调失败。
    with httpx.Client(timeout=90) as client:
        for target in (f"{BASE['control']}/health/ready", f"{BASE['governance']}/health/ready",
                       f"{BASE['runtime']}/api/v1/health/ready", f"{BASE['tool']}/api/v1/health/ready"):
            wait_for(client, target)

        ensure_desktop_release(client)

        manage = management_headers(TENANT)
        # A restart can overlap two integration checks in the same second. A random
        # suffix prevents the fixtures from producing a misleading publish 409.
        agent = f"general-e2e-{uuid.uuid4().hex[:12]}"
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
            "X-Tenant-Id": TENANT, "X-User-Id": "e2e-user",
            "X-Rag-Agent-Key": "local-rag-service-key",
        })
        assert persisted["status"] == "COMPLETED"
        assert persisted["context"]["snapshot_id"] == version["version_id"]

        request(client, "POST", f"{BASE['tool']}/api/v1/tools/create_ingestion_job/invoke", headers={
            "X-Tool-Gateway-Key": "local-tool-gateway-key", "X-Tenant-Id": TENANT,
            "X-User-Id": "e2e-user", "X-Permissions": "ingestion:write", "X-Request-Id": "e2e-tool-request",
            "X-Idempotency-Key": f"e2e-tool-{run['run_id']}", "X-Trace-Id": trace_id,
            "X-Run-Id": run["run_id"], "X-Agent-Id": agent, "X-Agent-Version": "1.0.0",
            "X-Snapshot-Id": version["version_id"],
            # 写工具必须来自已准入的计划步骤；这些关联 ID 证明它不是脱离 Runtime
            # 状态机的任意副作用调用，并会原样进入工具审计事件。
            "X-Root-Task-Id": run["run_id"],
            "X-Business-Operation-Id": f"business-{run['run_id']}",
            "X-Operation-Id": f"operation-{run['run_id']}",
            "X-Plan-Id": f"plan-{run['run_id']}",
            "X-Plan-Admission-Id": f"admission-{run['run_id']}",
            "X-Step-Id": "step-create-ingestion-job",
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
