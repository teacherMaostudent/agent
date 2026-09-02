# Platform Contracts

跨语言、跨服务的版本化契约。JSON Schema 是唯一规范来源；HTTP、事件总线和日志
都必须使用同一组语义字段，不能由调用方临时拼装。

## 为什么要独立维护契约

服务物理拆分后，最危险的依赖不是网络调用本身，而是同一个字段在不同服务中逐渐产生不同含义。
Platform Contracts 将执行上下文、错误和事件定义成可版本化资产，使 Python、Java、Web BFF、同步 HTTP、
异步事件和离线回放共享同一语义。它是契约包，不是可部署服务，也不承载业务逻辑或运行状态。

## 契约

| 文件 | 用途 |
| --- | --- |
| `schemas/execution-context.v1.json` | 一次 Agent Run 的身份、版本、Deadline 与预算 |
| `schemas/error-envelope.v1.json` | 稳定的跨服务错误模型 |
| `schemas/governance-event.v1.json` | 送入事件总线和 Governance 的审计/评测事件 |
| `schemas/session-event.v1.json` | Session Runtime 的追加式语义执行事实，供 Runtime API 与 Agent Lab 回放使用 |

## HTTP 映射

`ExecutionContext` 中的高频字段映射为以下内部 Header：

```text
X-Request-Id
X-Trace-Id
X-Run-Id
X-Session-Id
X-Parent-Session-Id
X-Agent-Id
X-Agent-Version
X-Snapshot-Id
X-Deadline-At
X-Attempt-Budget-Remaining
```

`tenantId`、`userId` 和权限声明必须来自可信身份层，而不是模型输出。服务身份使用
mTLS/JWT 或独立内部 API Key；不得把长期用户凭证放进 Prompt 或治理事件。

## 兼容性规则

- 文件名中的主版本号变更表示不兼容变更。
- 同一主版本只允许新增可选字段，不能修改字段语义或删除字段。
- 所有事件均携带不可重复的 `eventId`，消费者按该字段幂等。
- `deadlineAt` 是绝对 UTC 时间；下游只消耗剩余时间，不得重新开始固定超时。

新增字段应先更新 Schema 和契约测试，再升级生产者，最后升级消费者。破坏性变更必须创建新主版本并设置迁移窗口，
不能在原 Schema 中直接改名。消费者应忽略同一主版本未知的可选字段，但必须拒绝缺少必填身份、版本或预算字段的请求。

## Multi-Agent 标识关系

`runId` 表示一次对外业务执行，`sessionId` 表示一个可恢复执行会话，`parentSessionId` 表示协作父子关系，
`agentId/agentVersion/snapshotId` 固定实际执行者及其不可变配置。多个 Agent 可以共享 Trace 形成一条业务链，
但不能共享未声明的权限、预算或幂等键。治理事件必须同时保留业务根任务和实际执行 Agent 的关联标识。

执行上下文还携带 `releaseId`、`releaseStage`、`releaseProjectionRevision`、`trafficPolicyVersion` 与
`sideEffectPolicyVersion`。这些字段由 Control Plane 发布投影生成，调用方不可自报。它们把同一 Snapshot
在 Shadow、Canary 和 Production 中的风险执行事实区分开来，同时保持跨服务 Trace 可比较。

## 本地端到端门禁

启动完整拓扑后，从仓库环境运行黑盒测试：

```powershell
docker compose -f compose.platform.yaml up --build -d
& .\rag-agent-service\.venv\Scripts\python.exe .\scripts\platform_e2e.py
```

测试会创建 Release、让 Runtime 解析不可变 Snapshot、验证耐久 Run 记录、使用同一 Execution Context 调用
Tool Gateway 写工具，并断言前后 Governance 事件。契约测试验证语义兼容，不能替代各服务的业务测试。

## 实验与发布约束

Agent Lab 的实验计划、冻结快照与 release evidence 属于其服务 API，不是通用 ExecutionContext
的一部分。它们必须通过 Control Plane 和 Governance 已版本化的 HTTP/事件契约关联，不能把实验
内部字段临时塞入业务 Header。Model Lab 的训练计划、模型卡和工件 manifest 同理：它们是模型
生命周期资产，不应由在线 Runtime 直接读取未发布的草稿。

## 安全与数据最小化

契约只定义允许传播的字段，不意味着所有字段都应进入 Prompt、日志或前端。Token、密钥、原始身份声明和敏感正文
必须留在其安全边界内；事件和错误信封只携带脱敏摘要、分类标签与关联 ID。Schema 校验通过也不等于权限通过，
接收服务仍必须验证工作负载身份、租户、对象关系和策略。
