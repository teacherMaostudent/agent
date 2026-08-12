# Agent Platform 架构总览

## 目标与边界

平台把一次 Agent 执行定义为“已发布的不可变快照在受控权限、预算、证据和审计约束下的运行”。
业务差异通过 Control Plane 的 Agent 定义、知识源、工具目录、权限和发布策略表达，而不是复制一套 Runtime。

## 服务地图

| 类型 | 服务 | 负责 | 不负责 |
| --- | --- | --- | --- |
| 线上 | Control Plane | 定义、版本、发布快照、发布编排 | Agent Loop 与业务副作用 |
| 线上 | Agent Runtime | Planner、LangGraph、Harness、审批/预算/状态 | 模型厂商协议、知识索引 |
| 线上 | Context Service | 记忆组织、排序、Token 分配 | 决策与工具执行 |
| 线上 | RAG / Ingestion | 证据检索、ACL、索引版本、摄取 | 决定下一步动作 |
| 线上 | LLM Gateway | 模型路由、调用、限额、成本与安全策略 | Agent 业务编排 |
| 线上 | Tool Gateway | 工具目录、鉴权、审批、幂等和安全执行 | 业务流程决策 |
| 线上 | Governance | 审计、评测资产、Judge、质量 Gate 与合规 | 在线 Agent 执行或发布 |
| 离线 | Model Lab | 训练/量化实验、模型卡与模型评测 | Agent 回放与正式发布 |
| 离线 | Agent Lab | 冻结快照、离线回放、失败样本和基线比较 | 训练模型、线上流量、定义 Gate |

`platform-sdk` 提供跨服务契约、HTTP 客户端、统一错误、脱敏与追踪传播；`platform-infra` 提供
OIDC/mTLS、OPA、遥测、Schema Registry 和存储适配。应用服务不得互相 import 内部模块。
Temporal、PostgreSQL、Kafka、对象存储、OpenSearch/向量库、OIDC Provider 与 OPA 是基础设施，不是业务服务。

## 两条生命周期

### 线上执行

1. Runtime 验证调用方身份并从 Control Plane 解析 Agent 发布快照和已冻结的 `runtime-snapshot/v1` Artifact。
2. Control Plane 在发布事务内完成 Artifact 编译；Runtime 在生产模式只校验哈希并加载它。Artifact 将 Graph、
   Prompt、知识、工具、模型与上限编译为可执行计划及受限
   `workflow-policy/v1`；未知节点类型、非法迁移与能力漂移会 fail-closed。
3. Runtime 经 Context/RAG 获取排序后的记忆和 ACL 证据；RAG 可选时明确降级为 memory-only。
4. Harness 只负责解析 Release、加载 Snapshot、创建执行上下文、选择 Executor 以及运行/恢复/取消；它不实现
   Intent、RAG、Prompt Assembly、LLM Routing、Tool Auth 或领域逻辑。Executor 再由 LangGraph 在计划允许的边界内
   运行状态机；LLM Gateway 调模型，Tool Gateway 执行受控工具。
5. 高风险工具转为审批中断；预算、取消和幂等状态由 Runtime 管理。
6. 各服务把业务事务和 Outbox 事件一起提交；Governance 异步审计、评测和生成发现。

发布 Graph 不会加载租户自定义 Python 节点。它只能声明既有安全节点的迁移，LangGraph 在模型决策、
检索和工具执行后检查该策略。Harness 按已部署的 `runtime_executor` Profile 选择执行器；生产环境
未知 Profile 拒绝，不能因未知 Agent 静默回退默认图。

Graph 分支条件同样是受限 DSL，而不是 Python 表达式。Control Plane 发布时和 Runtime 编译时都会
验证字段、运算符、字面量与分支完整性；仅 `decision.action`、意图、证据计数、工具成功状态和预算
事实可参与判断。Runtime 只用这些白名单事实评估快照中已编译的结构化条件，未命中或多条命中均
不能由模型自行选择替代路径。

### 发布与实验

1. Control Plane 生成已版本化的 Agent 定义和 laboratory Release。
2. Agent Lab 为每个用例解析同一 Release 快照，并固定会话绑定，拒绝快照漂移。
3. Agent Lab 调 Runtime 回放，并把响应、引用和 Trace 交给 Governance Judge 与质量门禁。
4. Agent Lab 仅对“完成、快照冻结、Gate 通过”的实验生成内部 release evidence。
5. 启用 `CONTROL_PLANE_AGENT_LAB_REQUIRED=true` 后，Control Plane 校验 evidence 的租户、Agent、版本、
   laboratory 环境与 Judge Run，再执行正式发布 CAS/Saga。

Model Lab 走另一条模型生命周期：固定训练计划、数据指纹、基础模型版本和随机种子，产出模型卡和
评测结果；它可以影响 Control Plane 的模型路由，但不能替代 Agent Lab 的端到端回放证据。

## 已实现与上线前缺口

Runtime 与 RAG/Context 已实现进程、镜像、SDK 依赖边界；Control Plane 已能强制关联 Agent Lab evidence。Agent Lab
也已提供 PostgreSQL、Temporal Worker、租约、指数重试、持久化 DLQ、OIDC/mTLS 与 API/Worker 分离的生产代码路径。
本地开发可退回 SQLite 与同步队列。生产 HA 是否成立仍取决于部署侧的多副本、NetworkPolicy、可观测性、备份、
对象存储归档和跨区域故障演练，不能把 Compose 或单机代码误认为完整生产环境。
