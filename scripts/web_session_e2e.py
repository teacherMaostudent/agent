"""本地真实 OIDC/BFF 回归：只走授权码 + PKCE，不伪造 Cookie 或修改会话存储。

凭证只从 AUDIT_USERNAME/AUDIT_PASSWORD 环境变量读取，输出不包含密码、Token 或 Cookie。
默认只读；--submit-task 额外创建一个标注为审计的低成本普通任务，保留其审计记录。
"""
from __future__ import annotations

import argparse
import os
import time
from html.parser import HTMLParser
from urllib.parse import urlsplit

import httpx


class LoginForm(HTMLParser):
    """提取 Keycloak 登录表单；不猜测登录 action，不接触浏览器已有会话。"""

    def __init__(self) -> None:
        """初始化本轮登录表单解析状态，不共享已有登录上下文。"""
        super().__init__()
        self.action = ""

    def handle_starttag(self, tag, attrs) -> None:
        """只接受明确标识的登录表单。"""
        values = dict(attrs)
        if tag == "form" and values.get("id") == "kc-form-login":
            self.action = values.get("action", "")


def run(
    submit_task: bool = False,
    verify_identity_write: bool = False,
    verify_tenant_write: bool = False,
) -> None:
    """验证真实登录、页面资源、权限投影、只读业务页面、CSRF 与退出失效。"""
    base = "http://127.0.0.1:9010"
    username, password = os.environ["AUDIT_USERNAME"], os.environ["AUDIT_PASSWORD"]
    checks = 0

    def check(condition: bool, name: str) -> None:
        """仅输出不含用户数据的测试名称，失败时保留明确场景。"""
        nonlocal checks
        if not condition:
            raise AssertionError(name)
        checks += 1
        print(f"PASS {name}", flush=True)

    with httpx.Client(timeout=30, follow_redirects=True) as client:
        anonymous = client.get(base + "/api/session", headers={
            "X-User-Id": "admin", "X-Tenant-Id": "demo", "X-Permissions": "ops:read",
        })
        check(anonymous.status_code == 401, "anonymous_and_forged_headers_denied")
        page = client.get(base + "/")
        form = LoginForm()
        form.feed(page.text)
        target = urlsplit(form.action)
        check(target.scheme == "http" and target.hostname == "127.0.0.1" and target.port == 9110,
              "login_form_is_local_keycloak")
        # Keycloak 给回环开发地址签发 Secure Cookie，浏览器将 loopback 视为可信上下文，
        # HTTPX 不实现该例外。仅调整本测试新收取的 Cookie，不访问或伪造浏览器会话。
        # 不得把此适配用于远程域名、生产环境或 BFF 的业务 Session。
        for cookie in client.cookies.jar:
            if cookie.domain == "127.0.0.1" and cookie.path == "/realms/agent-platform/":
                cookie.secure = False
        page = client.post(form.action, data={"username": username, "password": password, "credentialId": ""})
        if page.status_code != 200 or not str(page.url).startswith(base + "/"):
            print(f"LOGIN_FAILURE_HTTP={page.status_code} PATH={page.url.path}", flush=True)
            if page.headers.get("content-type", "").startswith("application/json"):
                print("LOGIN_FAILURE_DETAIL=" + str(page.json().get("detail", "")), flush=True)
        check(page.status_code == 200 and str(page.url).startswith(base + "/"), "authorization_code_pkce_callback")
        session = client.get(base + "/api/session")
        check(session.status_code == 200 and session.json().get("authentication") != "local-development", "verified_oidc_session")
        for asset in ("/app.js", "/styles.css", "/execution-details.css"):
            asset_response = client.get(base + asset)
            check(asset_response.status_code == 200 and "Sign in to your account" not in asset_response.text, "asset:" + asset)
        forged = client.get(base + "/api/session", headers={"X-Tenant-Id": "other-tenant", "X-User-Id": "forged"})
        check(forged.json().get("tenant_id") == session.json().get("tenant_id")
              and forged.json().get("user_id") == session.json().get("user_id"), "jwt_identity_overrides_headers")
        identity_catalog = None
        tenant_catalog = None
        for endpoint in (
            "/api/workspace/runs?limit=5", "/api/review/runs?limit=5",
            "/api/console/services", "/api/console/agents", "/api/console/llm-quotas",
            "/api/console/model-route-releases", "/api/console/audit-exports", "/api/console/tenants",
            "/api/console/identity/users",
        ):
            response = client.get(base + endpoint)
            check(response.status_code == 200, "read:" + endpoint)
            if endpoint == "/api/console/identity/users":
                identity_catalog = response.json()
            if endpoint == "/api/console/tenants":
                tenant_catalog = response.json()
                check(any(item.get("tenant_id") == "demo" for item in response.json().get("items", [])), "bootstrap_tenant_catalog")
            if endpoint == "/api/console/services":
                items = response.json()["items"]
                check(bool(items) and all(item.get("http_status") == 200 for item in items), "all_console_health_targets")
        users = identity_catalog.get("items", []) if identity_catalog else []
        check(any(item.get("username") == "admin" and item.get("protected") for item in users),
              "protected_super_admin_visible")
        if verify_tenant_write:
            demo = next(item for item in (tenant_catalog or {}).get("items", []) if item.get("tenant_id") == "demo")
            updated_tenant = client.put(
                base + "/api/console/tenants/demo",
                headers={"Origin": base},
                json={
                    "display_name": demo["display_name"], "data_region": demo["data_region"],
                    "status": demo["status"], "reason": "OIDC E2E idempotent tenant lifecycle verification",
                },
            )
            check(updated_tenant.status_code == 200 and updated_tenant.json().get("tenant_id") == "demo", "tenant_catalog_write")
        if verify_identity_write:
            target = next(item for item in users if item.get("username") == "demo-operator")
            updated = client.put(
                base + "/api/console/identity/users/" + target["identity_id"],
                headers={"Origin": base},
                json={
                    "tenant_id": target["tenant_id"], "enabled": target["enabled"],
                    "roles": target["roles"], "permissions": target["permissions"],
                    "reason": "OIDC E2E idempotent authorization verification",
                },
            )
            check(updated.status_code == 200, "identity_authorization_write")
            refreshed = client.get(base + "/api/console/identity/users").json().get("items", [])
            persisted = next(item for item in refreshed if item.get("identity_id") == target["identity_id"])
            check(persisted["roles"] == target["roles"]
                  and persisted["permissions"] == target["permissions"],
                  "identity_authorization_persisted")
        blocked = client.post(base + "/api/workspace/runs", headers={"Origin": "https://untrusted.invalid"}, json={})
        check(blocked.status_code == 403, "cross_origin_write_denied")
        tasks = client.get(base + "/api/workspace/runs?limit=5").json().get("items", [])
        if tasks:
            detail = client.get(base + "/api/workspace/runs/" + tasks[0]["run_id"])
            check(detail.status_code == 200 and "execution" in detail.json(), "persisted_task_execution_projection")
        if submit_task:
            created = client.post(base + "/api/workspace/runs", headers={"Origin": base}, json={
                "agent_id": "general-agent", "environment": "local",
                "task": "本地审计测试：直接回答 2+2 的结果，不需要使用工具或知识库。",
            })
            check(created.status_code in (200, 202), "workspace_task_submit")
            run_id = created.json()["run_id"]
            deadline = time.monotonic() + 180
            while time.monotonic() < deadline:
                result = client.get(base + "/api/workspace/runs/" + run_id)
                if result.status_code == 200 and result.json().get("status") in {
                    "COMPLETED", "FAILED", "CANCELLED", "REJECTED", "LIMIT_EXCEEDED",
                }:
                    break
                time.sleep(1)
            check(result.status_code == 200 and result.json().get("status") == "COMPLETED", "workspace_task_completed")
            check(bool(result.json()["execution"].get("timeline")), "task_event_timeline")
            # COMPLETED 只表示运行终止成功；离线兜底文案不能冒充真实模型答对任务。
            detail = result.json()
            check("4" in detail.get("answer", ""), "arithmetic_answer_is_correct")
            check(detail["execution"]["model"].get("billed_llm_calls", 0) > 0,
                  "model_call_recorded_not_offline_fallback")
            check(bool(detail["execution"]["plan"].get("plan_id")), "planner_id_is_visible")
            print(f"AUDIT_RUN_ID={run_id}", flush=True)
        check(client.post(base + "/auth/logout", headers={"Origin": base}).status_code == 204, "logout")
        check(client.get(base + "/api/session").status_code == 401, "revoked_session_rejected")
    print(f"WEB_SESSION_E2E_PASSED={checks}", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--submit-task", action="store_true")
    parser.add_argument("--verify-identity-write", action="store_true")
    parser.add_argument("--verify-tenant-write", action="store_true")
    arguments = parser.parse_args()
    run(arguments.submit_task, arguments.verify_identity_write, arguments.verify_tenant_write)
