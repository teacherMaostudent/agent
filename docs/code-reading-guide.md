# 代码阅读与方法职责指南

本文不是目录清单，而是把一次请求、一次发布和一次离线评测映射到具体代码入口。阅读方法实现时，
应先确认“谁拥有状态”，再看“谁只做校验或适配”。方法注释统一回答五个问题：输入从哪里来、该方法
负责什么、不得越过什么边界、失败如何表达、是否改变持久化状态或产生外部副作用。

## 1. 先建立四层心智模型

| 层次 | 负责内容 | 不允许出现的行为 | 主要位置 |
| --- | --- | --- | --- |
| 发布控制层 | Draft、Spec、版本、快照编译、质量门禁、目标集群能力校验 | 执行在线 Agent Loop | `agent-control-plane/app/application/` |
| 在线执行层 | 解析 Release、计划准入、Graph/Workflow 执行、预算、审批恢复、取消 | 修改发布定义或绕过 Gateway | `agent-runtime/src/agent_runtime_service/` |
| 能力执行层 | Context、RAG、模型和工具的受控调用 | 决定整个任务下一步或拥有 Agent Session | `rag-agent-service/`、`llm-gateway/`、`tool-gateway/` |
| 治理实验层 | 审计、评测、校准、合规、离线回放和模型实验 | 直接同步控制在线主链路 | `agent-governance/`、`agent-lab/`、`model-lab/` |

`platform-sdk` 只保存跨服务契约和客户端，`platform-infra` 只保存身份、mTLS、OPA、遥测和存储适配。
如果共享包开始判断某个 Agent 的业务流程，说明边界已经被破坏。

## 2. 在线请求的完整代码路径

### 2.1 入口、身份与 Release 解析

1. Runtime API 接收请求并由统一身份中间件验证 OIDC；业务代码只读取已经验证并重建的身份上下文。
2. Harness 调用 `resolveRelease()` 和 `loadSnapshot()` 获取不可变发布事实，不读取 Draft，也不替 Planner
   决定任务路径。
3. Snapshot Loader 校验 Schema、摘要、Profile、Capability Manifest 和版本绑定。任何未知配置都
   fail-closed，不能为了“尽量运行”退回默认模型或默认工具。
4. Harness 创建 `ExecutionContext`，冻结 tenant、user、release、snapshot、trace、deadline、budget 和
   permissions，随后只根据已部署 Executor Catalog 解析执行器。

建议阅读顺序：

| 顺序 | 文件 | 重点方法或类型 | 观察点 |
| --- | --- | --- | --- |
| 1 | `agent-runtime/src/agent_runtime_service/api/routes.py` | 运行、恢复、取消接口 | HTTP 层只转换身份与错误，不复制状态机 |
| 2 | `agent-runtime/src/agent_runtime_service/runtime/harness.py` | `resolve_release`、`load_snapshot`、`run`、`resume`、`cancel` | Harness 是否仍限制在七项门面职责 |
| 3 | `agent-runtime/src/agent_runtime_service/runtime/snapshot_loader.py` | 快照加载与完整性校验 | 配置缺失、摘要漂移、能力缺失如何在执行前失败 |
| 4 | `agent-runtime/src/agent_runtime_service/runtime/executor_catalog.py` | Profile 与执行器解析 | 发布要求与目标集群部署事实是否一致 |
| 5 | `platform-sdk/platform_sdk/contracts/execution.py` | `ExecutionContext` | 跨服务携带哪些 ID、预算和授权事实 |

### 2.2 Planner、计划准入与 Graph

Planner 产生的是 `ProposedExecutionPlan`：它表达意图、步骤和候选 capability，但没有执行权。
`PlanAdmissionService` 根据快照允许列表、预算、风险、依赖和最大步骤数生成
`AdmittedExecutionPlan`。Executor 只能执行准入后的 operation；Tool Gateway 还会在副作用边界
重新核对 operation、step、plan 和 admission 标识。

| 组件 | 负责 | 不负责 | 失败语义 |
| --- | --- | --- | --- |
| Planner | Intent/Entity/Source/Complexity/SLA/Cost 分析，提出候选步骤 | 直接调用工具、批准自身计划 | 结构化规划错误，不产生副作用 |
| Plan Admission | 校验步骤、依赖、能力、风险、预算和授权，冻结准入计划 | 改写用户目标、选择未发布 Provider | 任一越权即拒绝整个计划 |
| LangGraph Executor | 按已编译 Graph 推进节点和受控条件 | 接受任意 Python 条件或动态导入节点 | 未知节点/条件/迁移在启动前失败 |
| ReAct Loop | 在单个获准步骤内进行有限 Thought/Action/Observation | 改变顶层 Owner 或无限扩展步骤 | 达到轮次、成本或截止时间即停止 |
| Temporal Executor | 保存长期 Workflow 状态、Timer、Signal、重试和恢复 | 替 Planner 决策或替 Gateway 授权 | Activity 可重试，业务拒绝不可盲重试 |

关键实现集中在 `agent-runtime/src/agent_runtime_service/runtime/`。阅读时特别区分：Graph 的条件规则是
发布期编译的受控表达式，Planner 的输出是运行期建议，Tool Authorization 是操作提交前的最终决定。

### 2.3 Context、RAG 与 Prompt

Context Service 拥有会话历史和上下文组装，RAG Service 拥有知识检索与证据，Runtime 拥有本次决策。
三者不能合并成一个“万能 Prompt Builder”。

1. Context 按 tenant/session 隔离读取消息，并基于角色、时间、相关性、来源可信度进行联合排序。
2. Token Budget 分别分配给系统约束、历史、证据和当前请求；裁剪必须保留解释信息，不能只返回拼接文本。
3. RAG Query 根据受控 Retrieval Policy 执行 BM25、向量、SQL 或文件扫描组合；检索策略来自发布快照，
   模型只能提出 Query Plan，不能绕开 ACL。
4. 可选 RAG 失败时返回带原因的 `memory-only` 降级；强制知识任务失败关闭。
5. Prompt Assembly 使用分段结构区分指令、历史和不可信证据；证据进入模型前经过注入检测、脱敏和长度上限。

代码入口：`rag-agent-service/apps/agent_context_service/`、`rag-agent-service/apps/rag_query_api/`、
`rag-agent-service/app/context/`、`rag-agent-service/app/retrieval/` 和
`agent-runtime/src/agent_runtime_service/runtime/prompt_assembly.py`。

### 2.4 LLM 与工具副作用

LLM Gateway 是模型执行策略点：校验 routeVersion/modelRevision，执行多供应商路由、限流、配额、
熔断、超时、成本计算、输出 Schema 和治理采样。它不拥有 Agent Plan，也不决定发布是否通过。

Tool Gateway 是所有业务副作用的最后安全边界。`ToolExecutionService.invoke()` 的顺序不可随意调整：

```text
解析固定工具版本
  → 校验租户可见性和主体权限
  → 核对 plan/admission/step/action
  → OPA 策略判定
  → 输入 Schema
  → 审批与请求摘要绑定
  → 幂等记录原子占用
  → 限流与熔断
  → 适配器执行和受限重试
  → 输出 Schema
  → 幂等完成、审计和 Outbox
```

审批通过不等于预算豁免；重试也不能生成新的业务幂等键。请求在适配器执行前失败时不得触发业务系统，
适配器执行后状态不确定时必须保留可对账事实，不能简单标记为“未执行”。

## 3. 发布生命周期的代码路径

1. `ControlPlaneService` 创建 Draft；更新使用 CAS，防止并发编辑静默覆盖。
2. Validation 将 Draft 转成正式 Spec，并检查模型、知识、工具、Skill、Workflow 和风险约束。
3. Snapshot Compiler 把 Spec 编译为 `runtime-snapshot/v1`，计算整体和组件摘要。
4. 发布前查询 Tool Catalog、Executor Catalog、Capability Manifest、索引版本和 Governance Gate。
5. Release Saga 写本地业务状态与 Transactional Outbox；CDC 将已提交事件投递到 Kafka。
6. Promote/Pause/Rollback 通过合法状态迁移和 CAS 更新，不直接修改历史 Snapshot。

建议从 `agent-control-plane/app/application/control_plane_service.py` 开始，再读
`snapshot_compiler.py`、`infrastructure/postgres_repository.py`、`infrastructure/temporal_release.py`。
Repository 的嵌套 `operation` 方法代表一个明确事务单元；它们的注释会说明业务记录与 Outbox 是否同事务。

## 4. Governance、Agent Lab 与 Model Lab

Governance 固定 Judge 模型版本、Prompt、Rubric、评测集、温度和输出 Schema。Judge 任一组成发生变化，
必须重新运行专家校准集；关键类别一致率、Kappa 和严重误判率不达门槛时，结果不能用于发布 Gate。

Agent Lab 冻结 Release Snapshot、数据集和 Runtime 版本后进行异步回放。它负责实验任务租约、Temporal
Worker、重试/DLQ、结果比较和 evidence 输出，不拥有正式发布状态。Model Lab 管训练或自部署模型实验、
工件、模型卡和评测；若平台只使用闭源 API，它仍可承担路由候选比较、量化模型登记和模型卡治理，
但不能声称训练了无法取得权重的模型。

| 服务 | 状态所有权 | 下游结果 |
| --- | --- | --- |
| Governance | 评测资产、Judge Run、Calibration Run、Quality Gate、合规发现 | 可验证 Gate Evidence |
| Agent Lab | Replay Experiment、Task Lease、Case Result、Baseline Comparison | Agent 发布证据候选 |
| Model Lab | Model Experiment、Artifact、Evaluation、Model Card | 模型路由发布证据候选 |
| Control Plane | 正式 Version、Snapshot、Release 和 Promotion 状态 | Runtime 可解析的发布事实 |

## 5. 数据一致性与故障恢复

| 场景 | 一致性策略 | 禁止做法 |
| --- | --- | --- |
| 业务状态 + 事件 | 同一 PostgreSQL 事务写 Transactional Outbox，Debezium CDC 投递 | 在业务事务中同步等待 Kafka |
| Draft/Release 并发更新 | CAS/version 检查 | 最后写入者无条件覆盖 |
| 工具写操作 | tenant + tool + idempotency key + request hash 原子占用 | 每次重试生成新键 |
| 一次性审批 | 审批绑定请求摘要并原子消费 | 复用审批执行不同参数 |
| 长期 Workflow | Temporal History、Activity Retry、Signal、补偿 | Web 进程内循环等待 |
| RAG 索引 | 不可变索引版本 + embedding contract | 在同一索引混用维度或模型 |
| 审计导出 | Hash Chain、WORM/对象存储与外部锚定 | 只保存在可修改业务表 |

## 6. 注释质量验收规则

方法注释不重复方法名，也不使用“处理某方法对应步骤”这类占位句。一个功能方法至少应从以下信息中
选择真正相关的内容，而不是机械填满模板：

- 输入与信任边界：参数来自已验证 OIDC、发布快照、模型建议还是数据库事实；
- 不变量：租户隔离、版本绑定、状态迁移、预算、截止时间、审批和幂等约束；
- 副作用：是否写数据库、调用外部系统、发布事件或只读；
- 失败语义：失败关闭、可选降级、可重试错误、不可重试业务拒绝；
- 一致性：事务、CAS、Lease、Outbox、补偿或重放边界；
- 返回值：结果是事实、建议、状态投影还是可执行授权。

`scripts/audit_comment_quality.py` 以只读方式检查缺失注释、纯英文注释和历史模板句，可直接接入 CI；它不会
根据方法名自动生成文字。评审仍需结合方法实现判断说明是否准确，不能把“通过脚本”当成质量结论。

## 7. 修改代码时的检查顺序

1. 先判断状态归哪个服务所有，跨服务数据是否应改为版本化契约或事件。
2. 修改领域模型和契约，再修改应用服务；API 与 Repository 不应各自复制业务规则。
3. 对写操作补齐身份、租户、权限、审批、预算、幂等和审计检查。
4. 对远程调用明确 timeout、retryable、deadline、attempt budget、circuit breaker 和降级条件。
5. 对状态迁移补齐 CAS/事务/Outbox，对长期任务补齐 Temporal 恢复语义。
6. 更新对应 README、架构说明和契约测试，最后运行 Ruff、pytest、Maven 与文档链接检查。
