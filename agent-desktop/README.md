# Agent Desktop

面向真实用户的受控 Agent 桌面执行台。它不是第二套 Runtime：Electron 主进程只负责
保存连接凭证、转发 Runtime API、消费 SSE 与读取用户显式选择的有界目录清单；计划、
模型、工具权限、审批、状态和审计仍由平台七个服务负责。

## 能力

- 提交交互任务并立即获得稳定 `run_id`；
- 显式选择 `local`、`staging` 或 `production` 发布环境，避免调试请求误命中正式 Release；
- 展示 Runtime Session Ledger 的实时事件；
- 将计划、快照、路由、证据、受控工具结果、成本/步骤和原始事件关联 ID 分区展示；
- 查看最终结果、暂停式人工审批、Steering、恢复与取消；
- 提供独立的空白“自定义发布任务”输入入口，并保留源码扫描、证据报告和文件整理预案
  三个可编辑演示模板；自定义内容只提交给选定 Agent/环境的 Active Release，不会由桌面端
  创建、修改或绕过 Release；
- Electron 启用 `contextIsolation`、`sandbox` 并关闭 Renderer 的 Node 权限；
- OIDC Token 只保存在 Electron 主进程内存，不写入前端存储；
- 本地目录只生成最多 250 项、深度最多 3 层的清单，不上传绝对路径或文件正文。
- 终态反馈保存在用户数据目录的 `feedback/feedback.jsonl`；凭据和 Windows 本地路径在
  写盘前脱敏，可按周筛选负反馈并回流为 Agent Lab Replay Case。
- 本地最近运行只保存 Run ID、状态和 Agent 索引；用户可显式导出单次运行的诊断 JSON，
  不会自动复制任务正文或证据到本地历史。
- 可在已验证 Runtime 连接后生成短时 Connector 配对码、确认配对、查看状态并撤销；配对码
  只在当前界面会话显示，服务端仅保存哈希。配对本身不等于 Tool Gateway 执行授权。
- 已确认的 Connector 每 30 秒向 Runtime 发送一次心跳；心跳仅用于在线诊断，不扩展工具
  权限，也不会自动恢复已撤销的设备。Runtime 在读取状态或签发 Grant 前会将超过 90 秒
  未心跳的设备标记为 `DISCONNECTED`。
- 已领取的 `controlled_scan` 任务必须由用户在桌面端再次点击确认后才会扫描当前显式选择
  的目录。扫描仅遍历 3 层、最多 100 个文件、单文件最多 1MB、最多返回 100 条命中；结果会
  脱敏常见 API Key 和邮箱并在回传前加哈希。
- 任务队列明确展示 `AWAITING_CONFIRMATION`、`EXECUTING`、工具审计完成以及 Artifact
  `PENDING / RETRY / DELIVERED / DEAD_LETTER` 状态；完成结果不会因 Context 短时故障而要求
  用户重复执行本机扫描。Runtime 的独立 Relay 会按租约与指数退避继续交付。

## 本地运行

```powershell
pnpm install
pnpm run dev
```

先启动平台 Runtime，并确保目标 Agent 的发布快照绑定了所需工具。桌面客户端默认调用
`POST /api/v1/agent/interactive-runs`；如果连接地址没有路径，主进程会自动追加 `/api/v1`。
一键启动脚本会幂等准备默认的 `demo / general-agent / local` 基础 Release；改用其他租户、
Agent ID 或环境前，必须先在 Control Plane 发布对应 Release。开发环境可使用本地持久队列，
生产环境应配置 Temporal。浏览器打开 `/api/v1` 只显示连接说明；它不是操作控制台。

Runtime 的 `/api/v1/agent/**` 是用户交互边界：本地可使用兼容身份 Header，生产必须由
OIDC/OPA 验证用户声明。内部目录与运维 API 仍要求工作负载凭据；不得把内部静态密钥
配置到桌面端。

## Windows 安装器

真实 Electron 交互回归入口为 `pnpm run e2e`。Playwright 已作为开发依赖锁定，命令会先生成
独立打包产物，再使用独立测试 profile/租户执行回归，不操作已有用户实例。已验证与未验证项见
[Desktop 交互测试记录](../docs/desktop-interaction-audit-2026-08-26.md)；组件测试通过不等于原生窗口和审批全链通过。

```powershell
pnpm run dist
```

安装器复用 `node_modules/electron/dist` 中由 Electron 安装脚本校验过的运行时，避免
打包阶段重复下载同一份二进制。若所在网络无法访问 GitHub，可在安装依赖前设置
`ELECTRON_MIRROR` 与 `ELECTRON_BUILDER_BINARIES_MIRROR`，指向组织批准的软件制品镜像。

桌面端默认使用 Chromium 沙箱；如果 Windows/显卡驱动组合导致 GPU 或 Renderer 以
`0x80000003` 崩溃，主进程会自动重启一次兼容模式。兼容模式仍保持上下文隔离、关闭
Renderer Node 权限并只暴露受控 Preload API，不会把 Runtime Token 交给页面脚本。
兼容性判定和子进程事件保存在用户数据目录的 `diagnostics/`；确认系统沙箱恢复后，可
删除 `chromium-sandbox-disabled.json`，或设置 `AGENT_DESKTOP_FORCE_SANDBOX=true` 复测。

## 边界

桌面端选择目录并不自动授予服务端文件权限。`controlled_scan` 只接受服务端预配置的
scope（例如 `workspace`），具体挂载与 `RAG_SCAN_ROOTS` 必须由部署管理员声明。这项限制
用于防止模型或 Renderer 把任意本地路径升级成可读权限。

默认 Compose 将仓库中的 `demo-workspace` 只读挂载为该 `workspace` scope，用于验证扫描
链路；它不是用户所选目录的映射。要扫描真实业务目录，必须由管理员单独配置最小范围的
只读挂载、`RAG_SCAN_ROOTS`、工具目录版本和对应 Release，再完成一次权限与审计验收。
