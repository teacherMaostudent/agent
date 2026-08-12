# Agent Runtime

`agent-runtime` 是平台的独立执行平面：它加载控制面已经发布的不可变快照，执行 Planner、LangGraph 和 Harness，并保存运行状态、预算、审批中断及异步任务。

它不读取 RAG、Context 或摄取服务的内部仓储、数据库和 Python 包。历史消息通过 Context HTTP API 获取；知识证据通过 RAG HTTP API 获取；模型与工具分别通过 LLM Gateway、Tool Gateway 调用。这样每个服务的 ACL、数据生命周期与工作负载身份边界都可以独立部署和审计。

Agent Lab 可用同一公开 Runtime API 回放已冻结快照；Runtime 仍只执行一次请求，不保存实验计划、
基线、发布证据或质量门禁。这些离线实验职责属于 `agent-lab`，评测规则属于 Governance。

## 运行职责

- 将用户请求与发布快照编译为受限的执行计划；
- 在 LangGraph 中执行受预算、可取消、可恢复的 Agent Loop；
- 将高风险工具调用转换为审批中断，审批恢复后继续同一运行；
- 将运行完成事件写入本地事务 Outbox，由治理链路异步投递；
- 在启用 Temporal 时作为 Worker 消费跨区域任务队列。

## Planner、Workflow 与 Harness 的标准边界

Runtime 将“可配置”限制在可审计的声明式边界内，而不是让发布快照执行任意代码：

1. **Snapshot Compiler** 将发布 Graph 编译为 `workflow-policy/v1`。它只接受
   `decision`、`retrieval`、`tool`、`answer`、`clarify` 五类受控节点，并校验入口、终点、
   可达性和每一条迁移；未知节点类型、工具作为入口、无效后继均 fail-closed。
2. **Planner** 根据本次请求生成 `ExecutionPlan`：意图、实体、来源、复杂度、SLA、成本、路由和
   检索档位。它不能新增快照未绑定的模型、知识源、权限或工具，也不能直接执行副作用。
3. **LangGraph** 是固定的可靠状态机与检查点实现。它在模型决策、检索和工具之后都验证
   Workflow Policy 的 cursor；模型只能建议 `RETRIEVE`、`TOOL` 或 `ANSWER`，不能改图。
4. **Harness** 是 API 与 Worker 唯一的执行门面，统一 `run/resume` 契约，并按快照中的
   `runtime_executor` Profile 选择本集群已部署的执行器。生产未知 Profile 一律拒绝；默认图
   仅在未启用 `RUNTIME_SNAPSHOT_REQUIRED` 的本地兼容模式可用。

Runtime 提供内部 `GET /api/v1/agent/capabilities`，只返回已部署的 `runtime_executor` Profiles 和
`RUNTIME_EXECUTOR_CATALOG_VERSION`。Control Plane 在生产发布前用它证明目标实例确实具备快照
要求的执行器；该接口必须使用服务身份、mTLS 与网络策略保护，不能暴露给终端用户。

每份 Execution Plan 都保存 `plan_hash`、Planner/Analyzer 版本、输入摘要和策略摘要，并随
Run 结果和 Governance Outbox 事件输出，供 Agent Lab 回放和线上审计关联。摘要不重复保存
用户原文或敏感 Prompt。

### Graph 条件 DSL

Graph Edge 的 `condition` 已实际参与 Runtime 路由，不再是仅保存的说明字段。为避免发布配置成为
代码执行入口，条件只能使用 `<field> <operator> <literal>`：

```text
decision.action == "RETRIEVE"
intent.confidence >= 0.80
evidence.count >= 1
tool.success == true
budget.remaining_ms > 3000
```

允许字段固定为 `decision.action`、`intent.name`、`intent.confidence`、`evidence.count`、
`tool.success`、`budget.remaining_cost_usd` 与 `budget.remaining_ms`；仅支持比较运算符和字符串、
布尔/数值字面量。`eval`、函数调用、模板、任意状态字段和无条件多分支均被 Control Plane 发布校验
与 Runtime Snapshot Compiler 双重拒绝。边会被编译为结构化条件并写入不可变快照，运行 Trace 可据此
解释为何某条边命中或未命中。

## 镜像依赖

镜像仅安装 `agent-platform-infra`、`agent-platform-sdk` 与 Runtime 自身。SDK 提供版本化契约、服务客户端、统一服务 Web 边界和脱敏规则；Infra 提供 OIDC/mTLS、OPA、Tracing 等基础设施适配器。Runtime Dockerfile 不再复制或安装 `rag-agent-service`。

## 本地验证

在仓库根目录执行：

```powershell
$env:PYTHONPATH="$PWD\platform-sdk;$PWD\platform-infra;$PWD\agent-runtime\src"
python -m pytest agent-runtime/tests -q
```

如果 IDE 将 `platform_sdk` 标为“未解析引用”，请为 Runtime 选择稳定的
`agent-runtime/.venv312` 解释器，然后执行一次本地安装：

```powershell
.\agent-runtime\.venv312\Scripts\python.exe -m pip install --force-reinstall --no-deps .\platform-infra .\platform-sdk
```

Windows 非 ASCII 工作目录下不建议使用 Hatch editable 安装，因为 `.pth` 路径可能被错误解码。随后重新加载项目；`platform-sdk` 必须作为源码根或已安装分发存在，不能依赖临时的终端 `PYTHONPATH`。

生产 Compose 为 Runtime、Context、RAG Query、摄取 API 和摄取 Worker 分配不同的 OIDC 工作负载客户端。部署时还应为每个工作负载签发独立 mTLS 证书；`X-Tenant-Id` 等 Header 只保留为迁移期兼容输入，不能作为生产身份信任根。

当 `RUNTIME_OIDC_ENABLED=true` 时，Runtime API 会要求经过 OIDC Middleware 验证的身份声明；业务路由
从已验证的 `tenant_id`、`sub`、`permissions` 读取身份，拒绝把调用方裸传的 `X-Permissions` 当作
授权依据。服务到服务调用应使用工作负载 Token + mTLS，而不是复用终端用户凭证。
# Harness 边界

`AgentHarness` 是 Runtime 的最小执行生命周期门面，只公开七项能力：
`resolve_release`、`load_snapshot`、`create_execution_context`、`resolve_executor`、
`run`、`resume`、`cancel`。它不持有 Agent Registry 的写权限，也不实现 Intent、
RAG、Prompt Assembly、LLM Routing、Tool Auth 或领域业务逻辑。

执行器 Registry 已收敛为启动期由 `AgentRuntimeContainer` 装配的只读
`ExecutorCatalog`；请求只能使用发布快照指定且本集群已部署的 `executor_profile`，
未知 Profile 会在进入 LangGraph 前拒绝。审批恢复读取持久化的编译发布计划，绝不从
Planner 的运行时业务计划猜测执行器。

## Runtime Snapshot 与 Executor Profile

Control Plane 在 Agent Version 发布事务中，将已校验的 `PublishedSnapshot` 编译为不可变
`runtime-snapshot/v1` Artifact，并把 Artifact 和原快照一起保存、一起计算版本内容哈希。生产
Runtime（`RUNTIME_SNAPSHOT_REQUIRED=true`）只校验哈希后加载该 Artifact，绝不在首个请求到来时
重新解释 Draft 或编译发布配置。

当前启动期只读 `ExecutorCatalog` 提供三种受控 Profile：

- `simple/v1`：一次性无状态短任务；不加载 Planner、RAG、LLM 或工具，且不能审批恢复。
- `declarative-langgraph/v1`：默认 Agent Loop；LangGraph 负责 Planner、Context/RAG/Tool 调用、预算和检查点。
- `temporal-workflow/v1`：长期可靠任务；只能经异步 `POST /runs` 提交。Workflow 在 `WAITING_APPROVAL`
  时保持存活，`POST /runs/{run_id}/resume` 发送审批 Signal，Worker 使用同一 LangGraph checkpoint 恢复。
