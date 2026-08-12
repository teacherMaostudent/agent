# Platform Contracts

跨语言、跨服务的版本化契约。JSON Schema 是唯一规范来源；HTTP、事件总线和日志
都必须使用同一组语义字段，不能由调用方临时拼装。

## 契约

| 文件 | 用途 |
| --- | --- |
| `schemas/execution-context.v1.json` | 一次 Agent Run 的身份、版本、Deadline 与预算 |
| `schemas/error-envelope.v1.json` | 稳定的跨服务错误模型 |
| `schemas/governance-event.v1.json` | 送入事件总线和 Governance 的审计/评测事件 |

## HTTP 映射

`ExecutionContext` 中的高频字段映射为以下内部 Header：

```text
X-Request-Id
X-Trace-Id
X-Run-Id
X-Session-Id
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
## Local end-to-end gate

Start the complete topology, then run the black-box test from the RAG virtual environment:

```powershell
docker compose -f compose.platform.yaml up --build -d
& .\rag-agent-service\.venv\Scripts\python.exe .\scripts\platform_e2e.py
```

The test creates a release, resolves its immutable snapshot at Runtime, verifies the durable Run record, invokes a Tool Gateway write tool with the same execution context, and asserts both Governance events.

## 实验与发布约束

Agent Lab 的实验计划、冻结快照与 release evidence 属于其服务 API，不是通用 ExecutionContext
的一部分。它们必须通过 Control Plane 和 Governance 已版本化的 HTTP/事件契约关联，不能把实验
内部字段临时塞入业务 Header。Model Lab 的训练计划、模型卡和工件 manifest 同理：它们是模型
生命周期资产，不应由在线 Runtime 直接读取未发布的草稿。
