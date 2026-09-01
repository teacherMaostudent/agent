# agent-governance

独立的 Agent 治理服务。它接收各服务已经发生的事件，持久化不可变审计记录，按租户策略生成评测发现（findings），并提供处置与合规汇总查询。

它**不是** `agent-control-plane`：控制面决定 Agent 的定义、版本和发布；治理服务拥有评测资产、Judge、质量门禁、线上样本和合规工作流。Control Plane 可以同步读取质量门禁结论，但治理服务不直接修改 Gateway 路由，也不执行 Agent 或业务工具。

在线请求中的计划准入由 Runtime Guard 完成，具体 Tool 动作的最终授权由 Tool Gateway
完成。Governance 接收 `tool.authorization.decided`、执行结果和 Trace 做异步审计，并负责
Golden、回归、Bad Case 与发布质量门禁；它不成为每次副作用调用的同步授权代理。

它也**不是** `agent-lab`：Agent Lab 负责冻结发布快照、编排离线回放和保存基线差异；本服务
继续是通用评测引擎与规则所有者。即使直接调用 Governance 的 Judge/Gate API，也不会自动获得
发布权限；启用 Agent Lab 门禁时，Control Plane 还会校验同一实验生成的内部 release evidence。

需要模型语义判断时，Governance 只通过 `GOVERNANCE_LLM_GATEWAY_BASE_URL`
调用 `llm-gateway`，不持有模型厂商密钥。核心接口位于
`/v1/governance/evaluations` 与 `/v1/governance/compliance`；Gateway 原有
`/admin/eval`、`/v1/feedback` 和 `/admin/compliance` 接口保留为兼容代理。

```mermaid
flowchart LR
    CP["agent-control-plane"] -->|"Outbox events"| GOV["agent-governance"]
    RT["agent-runtime"] -->|"run completion events"| GOV
    LLM["llm-gateway"] -->|"model request events"| GOV
    TOOL["tool-gateway"] -->|"tool execution events"| GOV
    GOV --> AUDIT[("Immutable audit log")]
    GOV --> FINDINGS["Findings and compliance reports"]
```

## 当前能力

### 评测自身的可复现性

每次 Regression 或 Judge Run 均先生成不可变 `EvaluationSnapshot`，并将其
ID 与 SHA-256 写入运行记录。Snapshot 冻结 Prompt、Golden Case、Rubric、模型
及其 provider revision、Gateway route version、temperature/top_p/max_tokens 和
`governance-judge-output/v1` JSON Schema。评测资产后续更新不会改变已完成
Run；若模型供应商未遵从结构化输出，Governance 会本地 fail-closed 拒绝结果。

- 幂等接收事件：同一 `event_id` 只会记录和评测一次。
- 记录不可变审计事件，按租户隔离并可使用游标查询。
- 规则评测：未获批准的高风险工具调用、违规模型、违规数据区域、缺少知识证据、成本或时延超限。
- 管理每个租户的治理策略；处置发现保留处理人、处理时间和说明。
- 策略变更同样作为 `governance.policy.updated` 事件写入审计日志。
- 输出按事件来源、风险等级和未处理发现汇总的合规报告。
- 以持久化作业执行 WORM 导出：先验证租户哈希链，再流式生成 Merkle 承诺和签名包，写入启用
  Object Lock 的对象桶；作业支持租约、多 Worker、指数重试、DLQ、显式重排和证明查询。

## 事件契约

生产者将 Outbox 事件转换为如下 HTTP 载荷后，投递到 `POST /v1/governance/events`。服务只消费事件，不向生产者回调或发出控制指令。

```json
{
  "event_id": "evt_01J...",
  "source_service": "tool-gateway",
  "event_type": "tool.execution.completed",
  "trace_id": "trace_01J...",
  "tenant_id": "tenant-a",
  "occurred_at": "2026-07-25T00:00:00Z",
  "payload": {
    "run_id": "run-123",
    "tool_name": "payments.refund",
    "risk": "write_high_risk",
    "approval_granted": false
  }
}
```

支持的首批评测事件是 `tool.execution.completed`、`llm.request.completed` 与 `agent.run.completed`。Runtime 还会写入 `agent.run.state_changed`：它带有 Run、Session、Trace、快照和前后状态，但不携带 Prompt 或用户正文，用于将长运行的状态迁移与最终评测结果关联起来。未知事件仍会完整审计，只是不触发内置规则。

## 快速启动

需要 Python 3.12：

```powershell
cd C:\Users\Administrator\Documents\AI工作\agent-governance
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
Copy-Item .env.example .env
uvicorn app.main:app --reload --port 8081
```

打开 `http://localhost:8081/docs` 查看 OpenAPI。审计查询与策略管理默认使用 `X-Tenant-Id`、`X-User-Id` 和可选的 `X-Roles: governance-auditor`。设置 `GOVERNANCE_ENFORCE_AUDITOR_ROLE=true` 后会强制该角色；设置 `GOVERNANCE_EVENT_INGESTION_KEY` 后，事件生产者必须携带 `X-Governance-Event-Key`。

也可通过 Docker Compose 启动：`docker compose up --build`。

## API

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `POST` | `/v1/governance/events` | 异步接收并评测事件 |
| `GET` | `/v1/governance/audit-events` | 查询不可变审计记录 |
| `GET` | `/v1/governance/findings` | 查询风险发现 |
| `POST` | `/v1/governance/findings/{id}/resolve` | 记录发现处置 |
| `GET/PUT` | `/v1/governance/tenant-policy` | 查询或更新治理策略 |
| `GET` | `/v1/governance/reports/compliance` | 查询租户合规汇总 |
| `POST/GET` | `/v1/governance/audit-exports` | 创建或列出租户 WORM 导出作业 |
| `GET` | `/v1/governance/audit-exports/{job_id}` | 查询对象键、摘要、Merkle Root 和签名身份 |
| `POST` | `/v1/governance/audit-exports/{job_id}/requeue` | 对 DLQ 作业执行显式审计重排 |

生产必须使用 `GOVERNANCE_WORM_SIGNING_MODE=kms`、独立 KMS 签名密钥和开启 Compliance Object
Lock 的专用桶。本地 Compose 的 MinIO + HMAC 只用于验证协议，不能作为监管级不可抵赖证明。

## 验证

```powershell
python -m ruff check .
python -m ruff format --check .
python -m pytest
```
# 双阶段连续治理

Governance 将发布质量拆成两个不可互相替代的阶段：

1. **Pre-production**：冻结 Snapshot、Golden/Red-Team Dataset、Judge、Rubric、模型修订和
   检索策略后执行离线评测，并输出 `PASS` 或 `FAIL` 的 GateDecision；Control Plane 以该证据创建 Release。
2. **Online**：按 `releaseId + snapshotId` 聚合 Shadow/Canary Trace，检查最小样本量、错误率、
   P95 延迟、成本和硬安全信号，输出 `HOLD`、`PROMOTE`、`PAUSE` 或 `ROLLBACK`。

Governance 只保存证据、计算指标和做决策；它不直接改变流量。Control Plane 通过
`POST /v1/releases/{release_id}/governance-action` 拉取 GateDecision，再校验租户、Release 与
冻结 Agent Version 一致后才调用自己的 Promote/Pause/Rollback 状态机。

`forbiddenTool`、`piiLeak` 与 `crossTenantAccess` 是线上 Hard Gate：任一非零即 `ROLLBACK`；
样本不足只会 `HOLD`，不会因偶然的 100% 成功率提升流量。人工确认的线上失败保存为 `BadCase`，
随后可作为 Golden Candidate 审核并进入下一轮回归集。
