# Agent Web BFF

浏览器同源边界，为 `agent-web` 提供 Workspace、Review 和 Platform Console API。它只做身份会话、
最小数据投影与后端代理，不复制 Runtime 状态机、Governance 规则或 Control Plane 发布逻辑。

## 为什么需要 BFF

浏览器不适合保存下游服务凭据，也不应理解七个服务各自的内部 API。BFF 将 OIDC 登录转换为短期、
同源、HttpOnly 会话，依据已验证身份聚合任务、审查、发布和治理数据，并用自己的工作负载身份访问后端。
这样既避免前端持有 Access Token，也避免为了页面展示把 Runtime、Control Plane 和 Governance 暴露到公网。

## 职责与明确不负责的内容

| BFF 负责 | BFF 不负责 |
| --- | --- |
| OIDC Code + PKCE 登录、登出、会话续期 | 签发企业身份或维护 IdP 密码 |
| CSRF、同源、CSP 与安全 Cookie | 用隐藏按钮替代服务端授权 |
| 将用户身份转换为可信下游调用上下文 | 接受浏览器自报 tenant/user/permission |
| 聚合 Workspace、Review、Console 所需数据 | 复制 Runtime 状态机或发布 Saga |
| 对高风险操作执行二次确认与近期认证检查 | 绕过 Governance 或 Control Plane 门禁 |
| 分页、字段裁剪和面向人的错误映射 | 永久保存任务、审计或 Agent 定义 |

## 请求链路

```text
OIDC IdP → /auth/callback → Redis Session
Browser + Session Cookie + CSRF → BFF
BFF → 身份/租户/对象关系校验 → mTLS 调用目标服务
目标服务再次授权并执行 → BFF 最小化投影 → Browser
```

对于 Workspace，BFF 代理任务创建、列表、详情、审批、Steering、取消和 Artifact 访问；对于 Review，
它只返回当前专家被分配且有权审查的对象；对于 Console，它聚合发布目录、质量门禁、路由、配额、租户、
成员和审计导出状态。不同页面共用会话，但不共用权限。

## Multi-Agent 中的作用

BFF 不调度 Agent。父 Run、子 Run、Session、协作关系和责任 Agent 均由 Runtime/Control Plane 返回，
BFF 只将这些内部对象投影为任务树、审查队列和运维拓扑。任何子 Agent 仍必须独立通过 Release、Snapshot、
权限和预算校验，不能因为父任务的页面已打开就继承无限权限。

## 安全边界

- 生产登录使用 OIDC Authorization Code + PKCE；`state`、`nonce` 和 PKCE verifier 一次性保存在 Redis。
- 浏览器只保存随机、HttpOnly、SameSite 会话 Cookie，Access Token 不写入 Local Storage。
- BFF 使用独立 mTLS 工作负载证书访问 Runtime、Governance 和 Control Plane。
- 发布、回滚、Release 操作及 Connector Artifact DLQ 重放需要独立权限、对象 ID 二次确认，并在生产校验
  最近 MFA/ACR，不能仅靠“隐藏按钮”授权。
- Content-Security-Policy 默认只允许同源资源；Artifact 签名 URL 只用于即时跳转，不落日志或持久化。

## 会话、租户与授权

登录账号来自 IdP；租户及成员角色来自平台管理域；`user_id` 是认证后形成的稳定主体标识。三者通过受控映射
关联，但不能互相替代。BFF 在每次请求中从服务端会话恢复声明，并将租户、主体、权限和 Trace 信息传给下游，
不读取表单或自定义 Header 中的同名字段作为可信身份。

生产环境的权限变更应使相关会话失效或在短时间内重新解析授权，避免管理员撤权后旧会话长期有效。目标服务
仍执行最终对象级授权，因此 BFF 缓存只能用于展示和加速，不能成为唯一许可来源。

## 失败语义与可观测性

- 未登录或会话过期返回明确的重新登录状态，不循环跳转；
- 权限不足返回 403，资源不存在或不可见按接口策略返回 404，不能用 409 掩盖身份错误；
- 下游超时、限流和不可用使用稳定错误信封，不向浏览器泄漏密钥、内部地址或堆栈；
- 每个代理请求传播 `request_id`、`trace_id` 和用户可见关联 ID，便于从页面定位到 Runtime 与 Governance；
- BFF 就绪检查应分别暴露关键依赖状态，但不能因为非关键 Console 依赖故障阻断普通 Workspace。

## 本地运行

```powershell
$env:PYTHONPATH="$PWD\platform-infra;$PWD\agent-web-bff\src"
python -m uvicorn agent_web_bff.main:app --host 127.0.0.1 --port 8010
```

本地模式允许关闭 OIDC，仅用于联调。生产必须设置 `WEB_BFF_ENVIRONMENT=production`，并配置 OIDC
Issuer/JWKS/Authorization/Token URL、Client、Redirect URI、Redis、Public Origin 与独立 mTLS 证书。
`GET /health/ready` 仅证明 BFF 进程存活，不替代下游依赖的 SLO 探测。

## 生产部署要点

- 至少两副本，Redis 会话共享，负载均衡器不依赖本机内存粘性；
- OIDC Redirect URI、Public Origin 和 Cookie Domain 必须使用最终 HTTPS 域名；
- BFF 使用独立工作负载证书，不与 Runtime、RAG 或其他服务共用私钥；
- 内外网路由分离，只开放 Web/BFF，后端服务通过网络策略限制来源；
- 对登录、任务提交、高风险 Console 写操作分别限流，避免统一限流误伤正常查询。

## 验证

```powershell
python -m pytest agent-web-bff/tests -q
python -m ruff check agent-web-bff/src agent-web-bff/tests
```

组件测试之外，还应验证完整浏览器登录、登出、账号切换、会话失效、租户切换、分页、权限撤销、审查指派和
发布二次确认。仅能打开首页不代表 BFF 的身份与授权链路已经闭环。
