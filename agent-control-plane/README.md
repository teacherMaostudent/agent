# agent-control-plane

> Governance quality gates used for release decisions must reference an
> immutable `EvaluationSnapshot`. Its frozen route version and model revision
> are enforced by LLM Gateway before any evaluator model call is made.

Agent 管理面与配置面。它负责定义、校验、版本化和发布 Agent，但**不执行 Agent
推理**。线上 Runtime 只读取已经发布的不可变快照，不直接读取会持续变化的草稿表。

模型路由发布编排也由 Control Plane 持有：读取 Governance 质量门禁，通过
Gateway 执行灰度策略，监控 Gateway 性能后提升或回滚。Gateway 原有
`/admin/releases` 仅作为兼容代理。

## 已实现能力

- 多租户 Agent 草稿与乐观并发控制
- Graph、Prompt、Tool、知识库、模型路由、运行上限的统一定义
- Graph 可达性、Prompt 变量、高危 Tool 审批、模型/数据区域策略校验
- 语义版本发布与 SHA-256 内容指纹
- 完整、不可变、自描述的 Runtime 快照
- 模型路由质量门禁、灰度、监控、自动提升与回滚
- 首次全量发布、确定性灰度、租户白名单和会话粘滞
- 灰度推进、暂停、回滚，以及回滚后会话安全重绑定
- 租户级模型、数据区域和最大灰度比例策略
- 与业务写入同事务的 Outbox 事件
- 可选强制关联 Agent Lab 的 laboratory 回放证据，防止无关 Gate 或草稿直接进入正式发布
- Runtime Executor Catalog：发布前确认目标环境存在匹配执行器，并由 Runtime 实例实时证明能力
- 管理端与 Runtime 端分离的鉴权入口
- OpenAPI、Docker、Compose、调用样例和自动化测试
- 独立 Skill Draft/Version 与 `VALIDATING -> CANDIDATE -> CANARY -> ACTIVE` 准入状态机
- 独立 Workflow Draft/Version/Release，支持零 Agent 执行工件
- 渐进披露 Skill Catalog，未选中前不返回 Prompt、Tool 和 Knowledge 绑定
- 随 Agent/Workflow 工件冻结 Capability Provider 目录和逐能力回退顺序
- 发布前校验 Active Skill、Tool Catalog 和 Workflow Provider 的精确版本/摘要
- 发布 Agent 时以 Tool Catalog 为权限、风险、审批和幂等事实源冻结工具绑定；发布 Skill
  时校验 Governance Profile 不得降低其内部 Tool 的真实风险

## 边界

```mermaid
flowchart LR
    Studio["Agent Studio / 管理后台"] --> CP["agent-control-plane"]
    GOV["agent-governance"] -->|"发布 / 暂停 / 回滚指令"| CP
    CP --> DB[("Control Plane DB")]
    CP --> OUT["Transactional Outbox"]
    OUT --> KAFKA["Kafka 配置事件"]
    RT["agent-runtime"] -->|"按 Tenant + Agent + Env + Session 解析"| CP
    CP -->|"ReleaseManifest + 不可变快照"| RT
    RT --> CTX["agent-context-service"]
    RT --> LLM["llm-gateway"]
    RT --> TOOL["tool-gateway"]
```

本服务不会运行 LangGraph/Workflow/Skill、保存聊天记录、检索知识、调用模型
或执行业务工具。

## 最重要的发布约束

1. `AgentDefinition` 是可编辑草稿，更新必须携带 `expected_revision`。
2. 发布前必须通过配置和租户策略校验。
3. `AgentVersion` 一经生成不可修改；相同 Agent 不能重复使用语义版本号。
4. `ReleaseManifest` 只引用已发布的 `version_id`。
5. 首个环境版本强制 100% 发布，避免不存在回退基线。
6. 新会话按稳定哈希进入灰度；已有会话固定到原 Release。
7. 回滚后，绑定到问题 Release 的会话在下次解析时切回上一稳定版本。
8. 配置变更与 Outbox 事件在同一数据库事务提交。
9. 启用 Agent Lab 门禁后，正式 Release 还必须引用完成的 laboratory 实验，且实验的租户、Agent、
   版本和 Judge Run 必须与发布请求完全一致。
10. 生产发布必须通过 Runtime Executor Catalog：目标环境声明 `runtime_executor` Profile，至少一个
    Runtime 实例的 `/api/v1/agent/capabilities` 返回同一 Catalog 版本和该 Profile。
11. `AgentVersion` 生成时会在同一事务内把 `PublishedSnapshot` 编译为 `runtime-snapshot/v1`
    Artifact；生产 Runtime 只加载并校验该 Artifact 的快照哈希，缺失或漂移即拒绝执行。

发布后的快照同时包含可观察版本号和完整配置：

```json
{
  "agent_version": "customer-service:1.8.2",
  "graph_version": "customer-service-graph:17",
  "prompt_version": "customer-service-system:17",
  "knowledge_version": "kb:39b45ef83d97",
  "tool_set_version": "tools:928d3e40c7f1",
  "model_policy_version": "balanced-routing:17",
  "spec": {
    "graph": {},
    "prompt": {},
    "tools": [],
    "knowledge": [],
    "model_policy": {},
    "runtime_limits": {}
  }
}
```

## 快速启动

要求 Python 3.12。

```powershell
cd C:\Users\Administrator\Documents\AI工作\agent-control-plane
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
Copy-Item .env.example .env
uvicorn app.main:app --reload --port 8080
```

打开 `http://localhost:8080/docs` 查看和调试 OpenAPI，或直接运行
[http/control-plane.http](http/control-plane.http) 中的完整发布流程。

也可以使用容器：

```powershell
docker compose up --build
```

## 身份与隔离

管理 API 默认要求：

```text
X-Tenant-Id: tenant-a
X-User-Id: architect@example.com
X-Roles: agent-admin
X-Trace-Id: trace-optional
```

本地默认不强制角色；生产设置 `CONTROL_PLANE_ENFORCE_ADMIN_ROLE=true`。Runtime API
可通过 `CONTROL_PLANE_RUNTIME_API_KEY` 启用独立的 `X-Runtime-Key`。

若将 Agent Lab 作为发布前回放门禁，生产还应设置：

```dotenv
CONTROL_PLANE_AGENT_LAB_REQUIRED=true
CONTROL_PLANE_AGENT_LAB_BASE_URL=https://agent-lab:8092
CONTROL_PLANE_AGENT_LAB_SERVICE_API_KEY=<service-secret>
```

Control Plane 会用服务密钥读取 Agent Lab 的内部 release evidence，并拒绝实验环境不是
`laboratory`、未完成、未通过 Gate，或 `quality_gate_run_id` 不一致的请求。该检查补充
Governance Gate，不取代 Governance 的评测和审计职责。

生产还必须挂载版本化 `runtime-executors.json`，并设置：

```dotenv
CONTROL_PLANE_RUNTIME_EXECUTOR_CATALOG_REQUIRED=true
CONTROL_PLANE_RUNTIME_EXECUTOR_CATALOG_PATH=/service/runtime-catalog/runtime-executors.json
CONTROL_PLANE_RUNTIME_EXECUTOR_CATALOG_SERVICE_API_KEY=<runtime-service-secret>
```

目录按环境列出可接收流量的 Runtime Cluster、base URL 和执行器 Profile。Control Plane 创建
Release 时不会只相信静态目录：它会调用候选实例 capabilities 接口，要求实例返回同一 Catalog
Version 和 `runtime_executor`。目录版本、摘要和目标 Cluster ID 会冻结到 Release 与 Outbox 事件。

数据库查询、版本、发布、策略和 Outbox 均以 `tenant_id` 隔离。

## API

| 方法 | 路径 | 用途 |
|---|---|---|
| `POST` | `/v1/agents` | 创建 Agent 草稿 |
| `PUT` | `/v1/agents/{id}/draft` | 按 revision 更新草稿 |
| `POST` | `/v1/agents/{id}/validate` | 执行发布前校验 |
| `POST` | `/v1/agents/{id}/versions` | 生成不可变版本 |
| `POST` | `/v1/agents/{id}/releases` | 首次发布或启动灰度 |
| `POST` | `/v1/releases/{id}/promote` | 增加灰度比例或全量 |
| `POST` | `/v1/releases/{id}/pause` | 暂停给新会话分配灰度 |
| `POST` | `/v1/releases/{id}/rollback` | 回滚上一稳定 Release |
| `GET` | `/v1/runtime/agents/{id}/resolve` | 为一次会话解析运行版本 |
| `GET` | `/v1/runtime/releases/{id}/snapshot` | 按 Release 获取快照 |
| `GET/PUT` | `/v1/tenant-policy` | 查询或更新租户策略 |
| `GET` | `/v1/outbox` | 查看待集成的配置事件 |

创建 Release 时，开启 Agent Lab 门禁的租户必须在请求中提供 `agent_lab_experiment_id` 与
`quality_gate_run_id`；两者均来自同一个已冻结的实验，而不是用户可任意填写的字符串。

Runtime Resolve 示例：

```http
GET /v1/runtime/agents/customer-service/resolve
    ?environment=production
    &session_id=session-001
X-Tenant-Id: tenant-a
X-Runtime-Key: ...
```

返回值包含选中的 `release_id`、`version_id`、分配原因和完整快照。Runtime 可以用
`release_id + content_hash` 作为本地缓存键。

## Outbox 与 Kafka

每个事件都包含：

```text
event_id
event_type
trace_id
tenant_id
aggregate_type
aggregate_id
occurred_at
schema_version
payload
```

本实现保存 Transactional Outbox，不在业务事务中同步调用 Kafka。生产环境建议使用
CDC/Kafka Connect 发布 `outbox_events`，消费端以 `event_id` 幂等。当前事件包括
`AgentCreated`、`AgentDraftUpdated`、`AgentVersionPublished`、
`ReleaseCanaryStarted`、`ReleasePromoted`、`ReleasePaused`、
`ReleaseRolledBack` 和 `TenantPolicyUpdated`。

## 数据存储

当前仓库使用 SQLite，目的是让领域规则、API 契约和端到端流程可在单机与 CI
零依赖运行。数据库边界保持独立，没有跨服务查表。进入多实例生产部署时应将
`SqliteRepository` 替换为 PostgreSQL 实现，并把高频 Session Binding 缓存在 Redis；
上层领域服务与 API 契约无需改变。

## 验证

```powershell
python -m ruff check .
python -m ruff format --check .
python -m pytest
```

测试覆盖配置校验、草稿并发冲突、快照不可变、多租户隔离、灰度会话粘滞、回滚重绑定、
租户模型策略和 Outbox 事件。
