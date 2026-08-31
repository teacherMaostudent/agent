# Web 与 Desktop 联调调试手册

本文是 Agent Platform 网页端（Agent Workspace、Agent Review、Platform Console）与
Agent Workbench Desktop 的本地调试手册。它描述的是当前仓库的真实入口、端口、认证模式、
任务链路、日志位置和故障排查顺序，适用于开发联调、演示验收和回归测试。

## 1. 调试对象与边界

```text
浏览器 ──同源 HTTP──> agent-web-bff :9010
Desktop ──HTTP──> Agent Runtime :8001
                         │
                         ├─ Control Plane :9002（Release/Snapshot）
                         ├─ Context :8002
                         ├─ RAG Query :8003
                         ├─ LLM Gateway :9000
                         ├─ Tool Gateway :9090
                         └─ Governance :9001
```

- Web 是统一入口，按会话权限展示 Workspace、Review 和 Console，不直接访问内部服务。
- Desktop 是受控执行客户端，默认提交到 Runtime；它不创建 Release，也不能绕过快照、权限、
  预算、审批或工具治理。
- Runtime 执行当前已发布的 Agent Release。自定义任务输入框提交的是执行请求，不是发布请求。
- 端口是宿主机调试入口；容器之间使用 Compose 服务名，不能把 `localhost` 当作容器地址。

## 2. 启动前检查

在仓库根目录 `C:\Users\Administrator\Documents\AI工作\agent` 打开 PowerShell。

```powershell
Set-Location "C:\Users\Administrator\Documents\AI工作\agent"
Get-Command docker
docker compose version
docker info
```

首次配置：

```powershell
Copy-Item .env.example .env
# 编辑 .env，至少设置真实模型调用所需的 DEEPSEEK_API_KEY；不要把密钥提交到 Git。
```

`.env` 不存在时平台仍可启动，但 LLM Gateway 只能离线运行，常见结果是
`No relevant evidence was retrieved in offline mode.` 或无法生成真实模型回答。

### 2.1 启动核心平台

```powershell
.\scripts\start-platform.ps1 -Action Start
```

常用操作：

```powershell
.\scripts\start-platform.ps1 -Action Status
.\scripts\start-platform.ps1 -Action Logs
.\scripts\start-platform.ps1 -Action Restart
.\scripts\start-platform.ps1 -Action Stop
```

统一启动器默认启用本地 Keycloak 身份联调；也可显式写出：

```powershell
.\scripts\start-platform.ps1 -Action Start -WithIdentity
```

需要同时启动 Agent Lab / Model Lab 时增加 `-WithLabs`。本地快速排查镜像或网络问题时可用
`-NoBuild`，但它只适用于镜像已经存在的情况。

### 2.2 健康检查

```powershell
$checks = @(
  'http://127.0.0.1:9002/health/ready',
  'http://127.0.0.1:9001/health/ready',
  'http://127.0.0.1:9000/actuator/health',
  'http://127.0.0.1:8001/api/v1/health/ready',
  'http://127.0.0.1:8002/api/v1/health/ready',
  'http://127.0.0.1:8003/api/v1/health/ready',
  'http://127.0.0.1:9090/api/v1/health/ready',
  'http://127.0.0.1:9010/health/ready'
)
$checks | ForEach-Object { try { (Invoke-WebRequest $_ -UseBasicParsing).StatusCode } catch { "$($_) -> $($_.Exception.Message)" } }
```

正常情况下 Web 应访问 `http://127.0.0.1:9010/`，Runtime API 根地址为
`http://127.0.0.1:8001/api/v1`。如果启用了身份覆盖层，Keycloak 地址为
`http://127.0.0.1:9110/`。

## 3. 网页端调试

### 3.1 三类页面

- **Agent Workspace**：普通用户创建任务、查看结果、引用、Artifact 和待审批事项。
- **Agent Review**：专家查看计划、证据、执行步骤、风险并进行转交、批注和标注。
- **Platform Console**：平台人员管理 Agent Version/Release、模型路由、配额、审计导出、
  DLQ 和身份授权。

三者由同一个 Web 应用承载，页面区域由权限控制。开发模式下页面顶部会显示本地身份输入区；
生产模式使用 BFF 的 OIDC/PKCE 会话，不能依赖 `X-User-Id` 等调用方 Header 冒充身份。

### 3.2 首次登录与本地身份

开发联调可在页面的本地开发身份区域填写租户、用户和逗号分隔权限，然后点击“应用”。例如：

```text
租户：demo
用户：web-user
权限：rag:read,rag:ingest:approve,file:scan,tool:invoke,ops:read,release:read,model:route:read,quota:read,audit:export
```

生产身份联调必须保持 OIDC，并确保 IdP 客户端的 redirect URI、scope 和 issuer
与 BFF 配置一致。退出登录或会话失效后，先清除站点 Cookie，再重新打开首页；不要反复刷新
旧的 `/auth/callback` 地址。

登录后侧栏应始终显示当前账号、租户、角色以及“切换账号”；OIDC 模式同时显示“退出登录”。
导航按已验证权限显示：业务用户只有 Workspace，`agent:review` 增加 Review，`ops:read` 增加 Console。
最高管理员可在 Console 的“用户与授权”中分配人类角色与权限模板；最高管理员角色和工作负载角色
不会进入可分配下拉框。
本地演示初始登录为 `admin / 970402`，仅允许在回环开发环境使用；需要共享环境时先在 `.env` 覆盖
`LOCAL_PLATFORM_ADMIN_PASSWORD`，生产必须改接企业 IdP、MFA 和密钥管理。

### 3.3 浏览器开发者工具

排查 Web 问题时按以下顺序检查：

1. **Network**：确认请求是否发到 `9010`，查看状态码、响应 JSON 和请求是否带 Cookie。
2. **Console**：关注 CSP、CORS、JavaScript 异常和未处理的 Promise。
3. **Application → Cookies**：确认 HttpOnly 会话 Cookie 是否存在、是否已过期。
4. **页面权限摘要**：确认按钮是“未授权”还是接口真正失败；禁用按钮通常是权限预期行为。

任务详情请求应能看到计划、Release/Snapshot、RAG Evidence、模型路由、工具调用、成本、
事件时间线和历史消息。某项为空时，先看对应响应字段是否为空，再判断是“没有发生”还是前端
渲染问题。

## 4. Desktop 调试

### 4.1 开发模式

```powershell
Set-Location "C:\Users\Administrator\Documents\AI工作\agent\agent-desktop"
pnpm install
pnpm run dev
```

如果 Windows 报 `spawn EINVAL`，通常是旧版开发脚本直接启动命令时的进程参数兼容问题；先使用
打包后的安装程序验证功能，或执行：

```powershell
pnpm run build
pnpm run dist
```

安装包位于 `agent-desktop\release\`。安装新版本后完全退出旧 Workbench，再重新打开，避免
旧 Renderer 缓存造成“代码已改但界面没变化”。

### 4.2 连接 Runtime

Desktop 左侧连接区填写：

```text
地址：http://127.0.0.1:8001/api/v1
租户：demo
用户：desktop-user
权限：rag:read,file:scan,tool:invoke
```

开发模式可不填 OIDC Token；生产模式必须使用有效 Bearer Token。点击“验证连接”后再提交任务。
地址必须是宿主机映射地址；如果 Desktop 在容器内运行，应改为 Compose 服务名对应的内部地址。

### 4.3 自定义任务与演示模板

- “自定义发布任务”允许输入任意任务描述，并绑定 Agent ID、发布环境和当前 Active Release。
- “演示模板”只提供可重复的示例任务。
- 空任务不能提交；输入内容后会显示 `CUSTOM` 标记。
- Desktop 不负责创建或修改 Release；要改变 Agent 配置，必须在 Console 完成 Draft → Version →
  Quality Gate → Release 流程。

## 5. 一次完整联调流程

1. 启动平台并确认八个健康检查通过。
2. 在 Web Console 确认目标 Agent 存在 Active Release 和 Runtime Snapshot。
3. 在 Web 或 Desktop 使用同一租户、用户和权限提交任务。
4. 观察任务状态：`QUEUED → RUNNING → WAITING_APPROVAL（如需要）→ COMPLETED/FAILED/CANCELLED`。
5. 打开任务详情，核对：Planner 计划、Harness Executor、Evidence ID、模型/routeVersion/
   modelRevision、Tool 参数与授权、Token/Cost/剩余预算、审批、Steering/取消和 Governance 事件。
6. 在另一端刷新任务列表，确认相同 `run_id` 可见；这验证了 Web 与 Desktop 使用的是同一 Runtime
   持久化事实，而不是各自的本地缓存。
7. 对结果 Artifact 做在线预览、版本信息和必要的下载校验。

## 6. 常见故障速查

| 现象 | 常见原因 | 处理顺序 |
| --- | --- | --- |
| `无法初始化工作区：会话登录已失效` | Cookie 过期或 OIDC 会话失效 | 清除站点 Cookie，回首页重新登录；检查 BFF Redis 和 issuer |
| `invalid_scope` | IdP 未启用请求的 scope | 使用已配置 scope，或在 Keycloak 客户端补充 scope 映射；不要只改前端 |
| `MissingRequiredClaimError` | Token 缺少 `iss/sub/aud/exp` 等必需声明 | 用当前 issuer 重新获取 Token，检查 BFF 的 audience/issuer 配置 |
| `409 Conflict` | 同一输入事件重复提交、版本冲突或幂等键冲突 | 查看响应 JSON 的 conflict 原因和 `run_id`；刷新任务，不要盲目重复提交 |
| `No relevant evidence was retrieved in offline mode` | 未配置 API Key、RAG 无索引或查询被降级到 memory-only | 检查 `.env`、RAG 健康状态、索引版本和摄取状态 |
| `Unable to complete within the configured runtime limits` | Runtime 时间预算、模型超时或工具重试耗尽 | 查看事件时间线和剩余预算；先用短任务验证，再检查 LLM/Tool 上游 |
| `controlled_scan` HTTP 500 | Tool Gateway 上游不可用、工具版本/权限不匹配 | 查看 Tool Gateway 日志、Catalog 版本、scope 和审计事件；确认工具实际被调用 |
| 宿主端口不可用 | 进程占用或 Windows excluded port range | `Get-NetTCPConnection -LocalPort 8001`；再执行 `netsh interface ipv4 show excludedportrange protocol=tcp` |
| Docker 镜像 `not found` 或 token 超时 | 镜像标签不存在、Registry/DNS/代理问题 | 核对 Compose 镜像标签；先单独 `docker pull`，修复 Docker Desktop 网络后再启动 |
| Web 能看见任务，Desktop 看不到 | 两端租户/用户不同，或请求指向不同 Runtime | 对比连接地址、租户、用户和 `run_id`；确认不是 Desktop 本地历史 |
| 新 UI 没出现 | 仍运行旧安装包或 Renderer 缓存 | 完全退出 Workbench，安装 `release` 下的新包后重启 |

## 7. 日志与证据定位

核心服务日志：

```powershell
docker compose --project-name agent-platform -f compose.platform.yaml logs -f agent-runtime
docker compose --project-name agent-platform -f compose.platform.yaml logs -f agent-web-bff
docker compose --project-name agent-platform -f compose.platform.yaml logs -f rag-query-api
docker compose --project-name agent-platform -f compose.platform.yaml logs -f tool-gateway
```

一次运行的排查主键是 `run_id`；跨服务串联时同时记录 `trace_id`、`root_task_id`、`session_id`、
`release_id`、`snapshot_id` 和 `event_id`。不要只凭页面摘要判断执行路径，应该以 Runtime 事件、
Tool/LLM Gateway 审计事件和 Governance Outbox 为准。

Desktop 的渲染器错误看开发者工具 Console；主进程错误看启动 Workbench 的终端输出。生产环境
不要在日志中输出 Token、API Key、原始 Prompt、原始文件内容或未经脱敏的模型响应。

## 8. 自动化回归

网页和服务回归：

```powershell
Set-Location "C:\Users\Administrator\Documents\AI工作\agent"
python -m pytest -q
```

Desktop 回归（真实打包 Electron）：

```powershell
Set-Location "C:\Users\Administrator\Documents\AI工作\agent\agent-desktop"
pnpm test
pnpm typecheck
pnpm run build
pnpm run e2e
```

验收标准至少包括：启动、安全策略、无效 Runtime 错误、自定义任务入口、模板切换、真实提交、
八类详情披露、反馈持久化、历史恢复、连接器配对/撤销、controlled_scan、缺失 Release、
Steering 后取消和空任务禁用。本机最新真实 Desktop E2E 基线为 `14/14`（`--without-native`）；原生目录选择/取消、
审批恢复、生产 OIDC、沙箱强制执行等场景需在对应环境额外验收。

## 9. 调试完成判定

一次联调只有同时满足以下条件才算完成：

- Web 和 Desktop 使用同一 Runtime、租户和有效权限；
- 任务能从提交走到终态，或明确进入审批/可恢复状态；
- 详情中能解释计划、证据、模型、工具、成本、审批和事件，而不是只有最终文本；
- 失败时页面显示可读原因，服务日志可按 `run_id/trace_id` 定位；
- 重复提交不会产生重复副作用，取消、重试和降级结果符合预期；
- Release/Snapshot、工具 Catalog、模型路由和索引版本均可追溯；
- 测试结果和环境限制已记录，不能用“页面打开了”代替端到端验证。
