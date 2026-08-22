# Agent Web BFF

浏览器同源边界，为 `agent-web` 提供 Workspace、Review 和 Platform Console API。它只做身份会话、
最小数据投影与后端代理，不复制 Runtime 状态机、Governance 规则或 Control Plane 发布逻辑。

## 安全边界

- 生产登录使用 OIDC Authorization Code + PKCE；`state`、`nonce` 和 PKCE verifier 一次性保存在 Redis。
- 浏览器只保存随机、HttpOnly、SameSite 会话 Cookie，Access Token 不写入 Local Storage。
- BFF 使用独立 mTLS 工作负载证书访问 Runtime、Governance 和 Control Plane。
- 发布、回滚、Release 操作及 Connector Artifact DLQ 重放需要独立权限、对象 ID 二次确认，并在生产校验
  最近 MFA/ACR，不能仅靠“隐藏按钮”授权。
- Content-Security-Policy 默认只允许同源资源；Artifact 签名 URL 只用于即时跳转，不落日志或持久化。

## 本地运行

```powershell
$env:PYTHONPATH="$PWD\platform-infra;$PWD\agent-web-bff\src"
python -m uvicorn agent_web_bff.main:app --host 127.0.0.1 --port 8010
```

本地模式允许关闭 OIDC，仅用于联调。生产必须设置 `WEB_BFF_ENVIRONMENT=production`，并配置 OIDC
Issuer/JWKS/Authorization/Token URL、Client、Redirect URI、Redis、Public Origin 与独立 mTLS 证书。
`GET /health/ready` 仅证明 BFF 进程存活，不替代下游依赖的 SLO 探测。

## 验证

```powershell
python -m pytest agent-web-bff/tests -q
python -m ruff check agent-web-bff/src agent-web-bff/tests
```
