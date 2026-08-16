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
4. **Capability Runtime** 在启动期冻结 Context、LLM、Retrieval、Tool、Workflow、Session、
   SubAgent 以及可选 Sandbox/Code Runner Provider。每项能力都有 `CapabilityManifest`（Provider、
   契约版本、工件摘要、隔离级别和执行器兼容范围）；Harness 在选择执行器前校验名称与 Profile
   兼容性。未部署或摘要漂移的能力均拒绝，不能在请求中临时注册插件或静默降级。
5. **Harness** 是 API 与 Worker 唯一的执行门面，统一 `run/resume` 契约，并按快照中的
   `runtime_executor` Profile 选择本集群已部署的执行器。生产未知 Profile 一律拒绝；默认图
   仅在未启用 `RUNTIME_SNAPSHOT_REQUIRED` 的本地兼容模式可用。

Runtime 提供内部 `GET /api/v1/agent/capabilities`，返回已部署的 `runtime_executor` Profiles、
能力目录版本、Manifest 摘要与 Provider 声明。Control Plane 在生产发布前用它证明目标实例确实具备
快照要求的执行器与外围能力；生产目录应固定 `capability_manifest_digest`，以拒绝同版本目录下的
Provider 漂移。该接口必须使用服务身份、mTLS 与网络策略保护，不能暴露给终端用户。

### Runtime Event Bus

`RuntimeEventBus` 是 Runtime **进程内**的提交后通知边界；`RuntimeInterceptionPipeline` 则提供
固定的 `pre_prompt → pre_model_request → post_model_response → pre_tool_execute → post_tool_result → post_step`
策略阶段。订阅者与 Hook 都只能在容器启动时装配，运行中不能注册插件；Hook 不得改写租户、用户、
Run、Trace 或 Snapshot 身份，异常一律拒绝操作。这样可把 Prompt 准入、模型治理、工具策略和结果净化
放在明确阶段，而不把它们塞入 Harness 或业务 Graph。

它不是 Kafka、Temporal 或 Governance 的替代品：跨进程投递、审计耐久性与重放仍由 Transactional
Outbox、CDC/Kafka Connect 和 Governance 负责。Session Event Store 使用同一生命周期契约并与状态
事务一起写入，不会另建一套状态事实来源。

### Session Event Store

每次 `RUN_STARTED`、取消、等待审批、完成或失败，都会与 `runtime_runs` 状态变更、必要时的
Governance Outbox 写入处于同一数据库事务；Graph 还会追加用户/助手消息、Prompt 组装、模型请求与
决策、工具调用和结果、子 Agent 委派等步骤事实。事件在 `(tenant_id, session_id)` 内分配严格递增的
`sequence`。`GET /api/v1/agent/sessions/{session_id}/events` 可按序号分页读取这一事件流。

模型可见正文不会以原文复制到 Runtime：事件只保存经过脱敏、长度限制的 `ModelVisibleMessage` 投影与
原文 SHA-256 摘要，`derive_model_messages()` 只能重建该受限投影。原始消息、证据和工具结果仍分别由
Context、RAG、Tool Gateway 在其 ACL、保留期和加密策略内保管。这样既能解释 Prompt 组成，又避免
Session 日志演变成无边界的明文数据仓库。

### SubAgent Manager

发布 Spec 可声明 `subagents` 绑定。`SubAgentManager` 只接受这些冻结目标，并按绑定的最大深度、
调用次数和预算比例切分父运行的剩余资源；未声明目标、超深度或超次数均 fail-closed。子 Agent
实际执行仍必须解析其自身 Release 和 Snapshot，经 Harness 与其独立权限/工具策略运行，不能继承
父 Agent 的任意工具或模型权限。

Graph 的模型动作新增受控 `SUBAGENT`：仅当发布 Workflow 当前允许 `tool` 角色时才可进入委派节点；
委派结果会作为脱敏、截断后的观察写回父 Graph。父 `tenant_id`、用户权限、Session 和 Trace 仅作为
身份关联传递，目标 Agent 的模型、知识、工具与审批策略一律从其自己的发布快照重新解析。
子运行会保存不可伪造的 `parent_run_id`，因此 Session Event Store 可同时按会话顺序和父子链路还原
一次委派。`POST /runs/{run_id}/followups` 只允许具有 `agent:subagent:control` 权限、且其指定父运行
确为目标运行祖先的调用方发起冷继续；新运行继承子会话并以旧子运行为直接父级。等待审批时仍必须走
`resume`，不能用 follow-up 绕过一次性审批。

### 可选 Code Runner

`code-runner/v1` 默认不部署。启用 `RUNTIME_CODE_RUNNER_ENABLED=true` 后，Runtime 才公布
`sandbox` 与 `code_runner` 能力，并只将代码交给 Tool Gateway 的版本固定
`controlled_code_runner` 工具；Runtime 本机没有 `exec`、Shell 或宿主文件系统回退。发布该 Profile
必须绑定该工具的精确版本，目标集群必须证明 Sandbox/Code Runner Manifest。真正的镜像、网络出口、
只读挂载、CPU/内存/时限与工件签名由远端 Sandbox Provider 强制执行。

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
