# Web、Desktop 与七服务本地联调指南

## 1. 本地能验证到什么程度

本地环境可以真实运行七个逻辑服务、Web/BFF、Desktop、PostgreSQL、Redis、MinIO 和两个 Relay，
并验证发布快照、Agent 执行、RAG、模型/工具事件、Artifact、人工审批、WORM 作业等业务链路。
它不是生产验收环境：统一启动器默认使用本地 Keycloak OIDC，部分工作负载仍可使用本地兼容配置、
离线模型模式和 HMAC 演示签名；
企业 IdP、KMS、云对象存储、Kafka/CDC、Temporal Global Namespace 和真实告警接收器需要单独环境。

| 能力 | 本地验证方式 | 本地结论 | 不能由本地证明 |
| --- | --- | --- | --- |
| 七服务与 Web | `start-platform.ps1` + 平台 E2E | 可真实验证进程、API、Release、Run 和治理事件 | 多副本 HA、生产网络策略 |
| 外部模型 | 根 `.env` 配置 DeepSeek Key 后执行任务 | 可验证 Gateway 路由、用量、成本和失败链 | 供应商 SLA 与生产额度 |
| Artifact 预览 | Workspace 打开任务产物 | 可验证文本白名单、限长、摘要和对象读取 | PDF/Office 隔离渲染 |
| Artifact 版本比较 | 同一 RootTask 创建同一 `logical_name` 的两个文本版本 | 可验证版本链和 unified diff | 大型二进制语义比较 |
| 模型路由 | Console 创建 Canary、观测和回滚 | 可验证 Control Plane 状态机和 Gateway 策略接口 | 真实生产流量质量提升 |
| LLM 配额 | Console 编辑 `*` 或用户配额 | 可验证租户/用户隔离和 Gateway 执行值 | 多区域 Redis 一致性 |
| WORM 导出 | Console 创建导出，Worker 写 MinIO 锁定桶 | 可验证 Hash Chain、Merkle、对象保留和 DLQ | 云 KMS 不可抵赖性、监管验收 |
| Desktop 扫描入 RAG | Desktop 扫描 → Web 审批 → Relay → Worker | 可验证人工门禁、幂等提交和索引终态 | 任意本机目录的隐式授权 |
| OIDC | 统一启动器默认叠加 `compose.identity.yaml` | 可验证真实 Keycloak、PKCE、JWT Claim、账号切换和服务端 Session | 企业 IdP 联邦、正式 MFA 策略 |
| 告警 | 校验 Prometheus/Alertmanager/Blackbox 配置 | 可验证规则和部署拓扑 | 未配置接收器时的真实通知送达 |
| 跨区域 Temporal | 配置校验和故障转移脚本 Dry Run | 可验证 fail-closed 参数和操作步骤 | 没有两套 Cluster 时不能做真实切换 |

## 2. 启动基础平台

前提是 Docker Desktop 已启动并处于 Linux Container 模式。PowerShell 必须位于仓库根目录：

```powershell
Set-Location "C:\Users\Administrator\Documents\AI工作\agent"
.\scripts\start-platform.ps1 -Action Start
```

脚本会先串行验证并缓存构建基础镜像和固定版本的 MinIO/`mc` 镜像。若此阶段失败，报错对象就是
尚未缓存的具体镜像；不要反复删除业务容器或数据卷。MinIO 使用官方已发布的
`RELEASE.2025-09-07T16-13-09Z`，`mc` 使用 `RELEASE.2025-08-13T08-35-41Z`。

脚本会启动七服务、Web/BFF、MinIO、摄取 Worker、Connector Artifact Relay、Artifact Ingestion
Relay 和 WORM Worker，并准备 `demo / general-agent / local` Release。默认还运行跨服务 E2E；只有排查
启动问题时才使用 `-SkipE2E`，不能把“服务能启动”等同于“联调通过”。

```powershell
.\scripts\start-platform.ps1 -Action Status
.\scripts\start-platform.ps1 -Action Logs
.\scripts\start-platform.ps1 -Action Restart -NoBuild
.\scripts\start-platform.ps1 -Action Stop
```

主要入口：

| 入口 | 地址 |
| --- | --- |
| Agent Web | `http://127.0.0.1:9010` |
| Agent Runtime | `http://127.0.0.1:8001/api/v1` |
| Control Plane | `http://127.0.0.1:9002/docs` |
| Governance | `http://127.0.0.1:9001/docs` |
| MinIO Console | `http://127.0.0.1:9101` |

## 3. Web Workspace 测试

打开 `http://127.0.0.1:9010`，本地身份使用 `demo / desktop-user`。依次验证：

1. 新建普通任务，选择 `general-agent / local`；确认返回稳定 Run ID。
2. 查看任务详情，确认能看到计划、Release/Snapshot、检索、模型、工具、成本和事件时间线。
3. 对运行中的任务测试 Steering、取消或人工审批恢复；终态不能继续执行控制动作。
4. 查看引用和 Evidence ID；离线模式没有证据时应明确显示不足，不能生成伪引用。
5. 打开文本 Artifact 在线预览；完整对象应显示 SHA-256 已验证，超限内容应显示已截断。
6. 同一逻辑产物存在上一版本时点击“对比上一版”，确认只显示同一版本序列的 unified diff。

本地 `admin` 还具有显式的 `run:tenant:read` 观察权限，因此 Workspace 会标注“管理员视图”，并显示
`demo` 租户内由 Desktop `desktop-user` 创建的任务。该权限只扩大读取范围：非本人任务仍不能取消、
Steering、共享、审批或改写其 Artifact 摄取决定。

## 4. Review 测试

使用具备 `agent:review` 且已被 Assignment 明确分配的用户进入 Review：

1. 查看计划摘要、风险、Evidence 和执行步骤。
2. 验证没有 `evidence:content:read` 或数据域权限时只能看到证据索引，不能看到正文。
3. 测试转交、共同审查、专家标签和拒绝原因回流。
4. 用未分配用户直接访问同一 Run，服务端必须返回 404，而不是只隐藏前端菜单。

## 5. Platform Console 测试

本地默认权限已经包含本轮新增操作：

1. **服务健康**：确认七服务状态；停止任一容器后刷新，观察不可用投影。
2. **Release**：验证 Draft、Version、Release、Promote、Pause、Rollback 的合法状态迁移。
3. **模型路由**：创建质量门禁约束的 Canary，执行一次 Monitor，再测试显式回滚。Console 不直接写
   Gateway 临时路由。
4. **LLM 配额**：先编辑 `*` 租户默认配额，再编辑 `desktop-user` 专属配额；执行模型任务并观察
   Token/成本拒绝。相同 user ID 在其他 tenant 中不能共享计数。
5. **WORM**：创建审计导出并刷新作业，预期 `QUEUED → RUNNING → COMPLETED`；结果应显示对象键、
   Merkle Root 和保留期。停止 MinIO 后可验证重试/DLQ，恢复后只允许显式重排。

## 6. Desktop 与 Web 联调

开发运行：

```powershell
Set-Location "C:\Users\Administrator\Documents\AI工作\agent\agent-desktop"
pnpm install
pnpm run dev
```

安装包验证使用 `pnpm run dist`，生成物位于 `agent-desktop/release/`。连接参数为：

- Runtime：`http://127.0.0.1:8001/api/v1`
- Tenant：`demo`
- User：`desktop-user`
- Agent：`general-agent`
- Environment：`local`

完整扫描与知识晋升链路：

1. Desktop 验证 Runtime 连接并完成短时 Connector 配对。
2. 显式选择本地目录；确认界面只生成有界相对路径清单，不上传绝对路径或整目录正文。
3. 提交“源码与日志扫描”任务，桌面领取 `controlled_scan` 后再次人工确认。
4. 查看 `AWAITING_CONFIRMATION → EXECUTING → 已审计 → Artifact DELIVERED`。
5. 回到 Web Workspace 打开同一 Run，确认 Artifact 状态为 `AWAITING_APPROVAL`。
6. 点击“批准进入 RAG”并填写理由；观察 `APPROVED → SUBMITTED / INDEX_QUEUED →
   INDEX_COMPLETED`。拒绝路径不得产生摄取任务。
7. 再发起检索任务，确认新文档只在摄取完成后可能成为 Evidence，且来源元数据保留审批人、审批 ID、
   RootTask 和 Artifact ID。

默认 Compose 的服务端 `workspace` Scope 只读挂载仓库 `demo-workspace`。Desktop 选择任意目录并不会
自动把该目录挂载给服务端；本机扫描与服务端受控扫描是两个不同数据边界。

## 7. Keycloak OIDC 联调

统一脚本默认启动 Keycloak 身份覆盖层和真实登录；显式的 `-WithIdentity` 仍可用于强调该模式：

```powershell
.\scripts\start-platform.ps1 -Action Start -WithIdentity
```

该参数会启动 Keycloak、导入本地 Realm，并让 Agent Web BFF 强制使用 OIDC/PKCE；此时 `9010` 会跳转到登录页。
只有服务开发或身份故障隔离时才使用 `-WithoutIdentity`，该模式不构成可交付的登录体验。
本地 Realm 仅请求 `openid agent-platform-claims`，后者包含租户、权限与角色 Claim；它不依赖企业 IdP 中
常见但本地未导入的 `profile`、`email` Scope。

需要验证真实浏览器登录时，停止普通本地拓扑后使用 Overlay：

```powershell
docker compose -f compose.platform.yaml -f compose.identity.yaml up --build -d
python scripts/platform_e2e.py --bootstrap-desktop
```

Keycloak 地址是 `http://127.0.0.1:9110`。本地 Keycloak 的管理账号仅用于 Realm 管理；不要把它和
Web 平台用户混用。平台管理员应使用已在 Keycloak 中创建的 `admin` 平台账号登录 `9010`。该账号拥有
`agent-user`、`agent-reviewer`、`platform-operator`、`governance-auditor` 四类业务角色以及不可转授的
`platform-super-admin`，因此可进入 **我的任务、审查中心、平台控制台**；它不拥有
`platform-workload`，后者是服务间委托身份，绝不能发给人类。
本机演示初始账号为 `admin`，默认演示密码为 `970402`；该弱口令仅服务于回环地址上的本地演示，
可通过 `.env` 的 `LOCAL_PLATFORM_ADMIN_USERNAME/LOCAL_PLATFORM_ADMIN_PASSWORD` 覆盖，禁止用于共享或生产环境。
本地最高管理员密码可由忽略提交的 `.env` 中 `LOCAL_PLATFORM_ADMIN_PASSWORD` 覆盖；生产账号密码只应
保存在企业 IdP 或密钥系统，不能写入 Realm JSON、Compose 文件或 Git。演示用户
`demo-operator` 首次登录必须修改临时密码。它们只用于本机协议验证，不能复制到生产。

应检查浏览器只保存随机 HttpOnly Session Cookie，Access Token 位于 BFF 的 Redis Session；退出后 Session
失效，伪造 `X-Tenant-Id`、角色或权限 Header 不能覆盖已验证 JWT Claim。
页面提供“退出登录”和“切换账号”；后者撤销当前 BFF Session，并以 `prompt=login` 重新进入 IdP。
最高管理员在 Console 的“用户与授权”中可选择常用权限模板并微调角色/权限；保存后目标用户的旧 IdP
会话会被撤销，下一次请求必须重新登录并取得新 Claim。
若浏览器保留了旧 Realm、旧 Scope 或旧签名密钥的会话并显示 401，请打开
`http://127.0.0.1:9010/auth/relogin`；它会在服务端撤销 BFF 的旧 HttpOnly Session 并自动回到新的
PKCE 登录流程，不依赖页面脚本或表单提交。

## 8. 自动化回归

各服务测试和静态检查不要求启动服务容器，但 Compose 配置校验仍需安装 Docker CLI。
当前基线覆盖 Runtime、RAG/Context/Ingestion、Control Plane、Governance、Tool Gateway、Web BFF、
Agent Lab、Model Lab、Platform Infra 和前端。项目根目录的统一入口：

```powershell
.\scripts\test-platform.ps1
# 仅针对运行中的本地开发服务，会创建合成测试数据和保留期内的审计导出对象
.\scripts\test-platform.ps1 -WithIntegration
# 先在本机环境安全提供 AUDIT_USERNAME/AUDIT_PASSWORD；不将凭证写入源码
.\scripts\test-platform.ps1 -WithOidc
```

各 Python 子项目使用自己的 `pyproject.toml`，统一入口会切换工作目录后运行，避免同名 `app` 包冲突。
直接执行 `platform_e2e.py` 默认只创建测试租户发布；加 `--bootstrap-desktop` 才初始化演示 Agent。
旧 `rag-agent-service/scripts/distributed_smoke.py` 需传 `--running-platform`，不再导入其他服务应用包。

最近一次实际结果、发现并修复的问题、源码挂载说明及未验证项见
[2026-08-26 审计报告](audit-2026-08-26.md)。该报告中的通过项不是生产全场景验收承诺。

## 9. 本机无法单独完成的验收

以下项目即使配置文件存在，也不能在单机上宣称完成生产验收：

- 企业 IdP 联邦、正式 MFA/ACR、SCIM 与人员离职回收；
- 云 KMS 非对称签名、真实 WORM 合规桶和法律保留策略；
- PostgreSQL/Redis/OpenSearch/Kafka 多副本、跨可用区恢复；
- 两套 Temporal Cluster 的 Global Namespace 复制、真实 Failover/Failback；
- PagerDuty、飞书或企业微信接收器的升级、静默和恢复通知；
- Windows/macOS 正式代码签名、自动升级和大规模终端兼容性。

这些能力应在预生产环境用独立身份、证书、域名和故障注入完成，不能用 Compose 单机截图代替。
