"""本地服务场景回归；只创建 audit-scenarios 测试数据，不改变用户配额、权限或正式发布。

覆盖工具鉴权、摄取 Worker、索引 ACL、S3 工件版本、Review 指派转交及 WORM 导出。
测试记录及锁定对象保留用于审计，不删除业务卷。此脚本只能用于默认 localhost 开发配置。
"""
from __future__ import annotations

import time
import uuid

import httpx

TENANT = "audit-scenarios-" + uuid.uuid4().hex[:8]
HEADERS = {"X-Rag-Agent-Key": "local-rag-service-key", "X-Tenant-Id": TENANT, "X-User-Id": "audit-user"}


def expect(response: httpx.Response, statuses=(200, 201, 202, 204)) -> dict:
    """严格区分 HTTP 成功和业务成功，避免下游错误被忽略。"""
    assert response.status_code in statuses, f"HTTP {response.status_code} at {response.url.path}"
    return response.json() if response.content else {}


def scan(client: httpx.Client) -> None:
    """只扫描演示目录，验证工具权限及未知 Scope 失败关闭。"""
    url = "http://127.0.0.1:9090/api/v1/tools/controlled_scan/invoke"
    headers = {**HEADERS, "X-Tool-Gateway-Key": "local-tool-gateway-key", "X-Permissions": "file:scan"}
    body = {"arguments": {"scope": "workspace", "pattern": "TODO"}}
    data = expect(client.post(url, headers=headers, json=body))
    assert data["status"] == "SUCCEEDED" and data["output"]["matches"]
    assert data["authorization"]["decision"] == "ALLOW"
    expect(client.post(url, headers={**headers, "X-Permissions": ""}, json=body), (403,))
    bad = client.post(url, headers=headers, json={"arguments": {"scope": "../not-allowed", "pattern": "TODO"}})
    assert bad.status_code in (400, 403, 422, 502) or (bad.status_code == 200 and bad.json()["status"] == "FAILED")


def ingestion(client: httpx.Client) -> None:
    """从上传到 Worker 完成再到实际检索；跨租户不得看到新文档。"""
    base = "http://127.0.0.1:8004/api/v1/ingestion"
    marker = "audit retrieval " + uuid.uuid4().hex
    result = expect(client.post(base + "/documents", headers=HEADERS, files={
        "file": ("audit-fixture.txt", (marker + "\nOnly synthetic test content.").encode(), "text/plain"),
    }))
    job_id, document_id = result["job"]["job_id"], result["document"]["document_id"]
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        job = expect(client.get(base + "/jobs/" + job_id, headers=HEADERS))
        if job["status"] in {"COMPLETED", "FAILED"}:
            break
        time.sleep(1)
    assert job["status"] == "COMPLETED", f"ingestion job {job_id}: {job['status']} {job.get('error', '')[:160]}"
    expect(client.get(base + "/jobs/" + job_id, headers={**HEADERS, "X-Tenant-Id": "other"}), (404,))
    query = {"query": marker, "tenant_id": TENANT, "user_id": "audit-user", "document_id": document_id}
    found = expect(client.post("http://127.0.0.1:8003/api/v1/query/search", headers=HEADERS, json=query))
    assert found["evidence"], "indexed document is not retrievable"
    denied = expect(client.post("http://127.0.0.1:8003/api/v1/query/search",
        headers={**HEADERS, "X-Tenant-Id": "other"}, json={**query, "tenant_id": "other"}))
    assert not denied["evidence"], "cross-tenant retrieval returned evidence"


def artifacts(client: httpx.Client) -> None:
    """验证真实 S3 内容、摘要、版本链、文本差异与租户边界。"""
    base = "http://127.0.0.1:8002/api/v1/context/tasks/" + TENANT + "/artifacts"
    first = expect(client.post(base + "/text", headers=HEADERS, json={"content": "audit version one", "logical_name": "audit-report"}))
    second = expect(client.post(base + "/text", headers=HEADERS, json={"content": "audit version two", "logical_name": "audit-report"}))
    assert second["version"] == first["version"] + 1 and second["previous_artifact_id"] == first["artifact_id"]
    preview = expect(client.get(base + "/" + second["artifact_id"] + "/preview", headers=HEADERS))
    assert preview["content"] == "audit version two" and preview["sha256_verified"] is True
    diff = expect(client.get(base + "/" + second["artifact_id"] + "/compare/" + first["artifact_id"], headers=HEADERS))
    assert "version one" in diff["diff"] and "version two" in diff["diff"]
    expect(client.get(base + "/" + second["artifact_id"] + "/preview", headers={**HEADERS, "X-Tenant-Id": "other"}), (404,))


def review(client: httpx.Client) -> None:
    """只使用平台 E2E 的合成 Run，检查指派、备注、转交与原审查人撤权。"""
    base = "http://127.0.0.1:8001/api/v1/agent"
    owner = {**HEADERS, "X-Tenant-Id": "e2e-tenant", "X-User-Id": "e2e-user", "X-Permissions": "run:review:assign"}
    runs = expect(client.get(base + "/runs?limit=20", headers=owner))["items"]
    fixture = next((item for item in runs if item["status"] == "COMPLETED" and item["agent_id"].startswith("general-e2e-")), None)
    assert fixture is not None, "run platform_e2e.py first"
    run_id = fixture["run_id"]
    reviewer = {**owner, "X-User-Id": TENANT + "-reviewer", "X-Permissions": "agent:review,run:review:comment,run:review:transfer"}
    url = base + "/review/runs/" + run_id
    expect(client.get(url, headers=reviewer), (404,))
    expect(client.post(base + "/runs/" + run_id + "/review-assignments", headers=owner,
                       json={"reviewer_id": reviewer["X-User-Id"], "reason": "synthetic audit assignment"}))
    expect(client.get(url, headers=reviewer))
    expect(client.post(url + "/comments", headers=reviewer, json={"message": "synthetic audit comment"}))
    assert expect(client.get(url + "/comments", headers=reviewer))["items"]
    next_user = TENANT + "-reviewer-next"
    expect(client.post(url + "/transfer", headers=reviewer, json={"reviewer_id": next_user, "reason": "synthetic transfer"}))
    expect(client.get(url, headers=reviewer), (404,))
    expect(client.get(url, headers={**reviewer, "X-User-Id": next_user}))
    expect(client.post(base + "/runs/" + run_id + "/cancel", headers=reviewer), (404,))


def worm(client: httpx.Client) -> None:
    """导出仅本测试租户的合成事件，验证 Worker 和对象锁证明；不触碰真实用户审计。"""
    base = "http://127.0.0.1:9001/v1/governance/audit-exports"
    headers = {**HEADERS, "X-Governance-Auditor-Key": "local-governance-auditor-key", "X-Roles": "governance-auditor"}
    created = expect(client.post(base, headers=headers))
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        job = expect(client.get(base + "/" + created["job_id"], headers=headers))
        if job["status"] in {"COMPLETED", "DLQ", "FAILED"}:
            break
        time.sleep(1)
    assert job["status"] == "COMPLETED", f"WORM {job['status']}: {job.get('last_error', '')[:160]}"
    assert job["result"]["object_key"] and job["result"]["merkle_root"] and job["result"]["retention_until"]
    expect(client.get(base + "/" + created["job_id"], headers={**headers, "X-Tenant-Id": "other"}), (404,))


def main() -> int:
    """独立执行所有场景，不让一个失败隐藏其余结果。"""
    failures = []
    with httpx.Client(timeout=40) as client:
        for scenario in (scan, ingestion, artifacts, review, worm):
            try:
                scenario(client)
                print("PASS " + scenario.__name__, flush=True)
            except (AssertionError, httpx.HTTPError, KeyError, ValueError) as exc:
                failures.append(scenario.__name__)
                print(f"FAIL {scenario.__name__}: {type(exc).__name__}: {exc}", flush=True)
    print(f"LOCAL_SCENARIOS={5-len(failures)}/5 TENANT={TENANT}", flush=True)
    return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(main())
