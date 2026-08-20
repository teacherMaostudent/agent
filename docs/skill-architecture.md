# Workflow / Agent / Skill 收敛架构

## 唯一分类方式

平台只有两种顶层主控：`WORKFLOW` 和 `AGENT`。Single/Sub/Multi-Agent 是
Agent 组织拓扑，Minimal/Plan/ReAct/Reflect/Graph 是 Agent 内部推理策略，
Skill、Tool、RAG、Memory 是可复用能力，Temporal 是持久调度方式。

```text
Execution = Owner + Topology + Reasoning + Capabilities + State + Governance
Owner = Workflow | Agent
Topology = None | Single | SubAgent | Multi-Agent
Capabilities = Tool | Skill | RAG | Memory | Agent | Human | Workflow
```

每个 `root_task_id` 只有一个 `orchestration_owner`。Workflow 调 Agent 时 Agent 只拥有
节点内局部推理权；根任务的重试、完成和取消仍属于 Workflow。Agent 同
RootTask 调 Workflow 时 Owner 仍为 Agent；`launch_mode=independent` 则创建新
RootTask 与 Workflow Owner。

## 发布面

Control Plane 分开管理 Agent、Skill 和 Workflow 的 Draft/Version/Release，三者不共用
草稿或状态机。Agent/Workflow 的 Provider 目录和每项能力的 `provider_order`
随工件冻结；Planner 只能声明 `capability_id`。

```text
Skill Draft -> Version(VALIDATING) -> CANDIDATE -> CANARY -> ACTIVE
                                      \-> QUARANTINED / RETIRED
ACTIVE -> DEGRADED / DEPRECATED / QUARANTINED / RETIRED
```

三个正向准入迁移均需 Governance 通过的 Quality Gate。Agent/Workflow 发布时再次
校验 Active Skill 的 `skill_id + version + artifact_digest`；Workflow Tool Provider 还要
通过 Tool Catalog，Agent Workflow Provider 必须指向精确已发布工件。

Skill 目录采用渐进披露：`/v1/skills/catalog` 只返回描述、能力、风险和版本；
选中且有精确绑定后，Runtime 才能加载完整 Prompt、Tool、Knowledge 和 Schema。

## 执行面

`ExecutionPlan` 保存 Owner、Topology、ReasoningPolicy、CapabilityPolicy、
DurabilityPolicy、GovernancePolicy、BudgetPolicy 和 VersionBindings。Agent 可在这些硬边界
内修订 `TaskPlan`，但不能改写权限、预算、Owner 或 Provider 回退链。

`GovernedSkillRuntime` 执行精确工件解析、激活策略、I/O Schema、组合深度/数量、
父预算切分、权限交集、受控执行和 Artifact 引用校验。`SkillContextBuilder`
只在统一 `ExecutionContext` 上增加 `skill_execution_id`，不创建 SkillSession。
Skill 的 LLM、Tool 和 Knowledge 访问仍经三个企业 Gateway/Service。

Control Plane 发布时对可见 Skill 目录执行身份、依赖环和 Schema 静态校验；
Resolver 实际激活多个 Skill 时才校验 `max_active_skills`、冲突、上下文总额和
工具重叠。因此一个 Agent 可看见较多 Skill Card，但不会把它们误认为全部同时激活。

## Capability Resolver

Provider 统一为 Tool、Skill、RAG、Memory、Agent、Human 或 Workflow。Resolver 先执行资格、
健康、权限、成本、SLA 和独立责任主体等硬约束，再依当前能力的 `provider_order`
选择。执行失败只在 Provider 显式声明 `fallback_safe=true` 时切换，避免未知
副作用重放。`CapabilityHealthMonitor` 将依赖实况投影为 AVAILABLE/DEGRADED/
UNAVAILABLE/QUARANTINED。

RAG Provider 还必须冻结 `knowledge_base + version + rag_index_version +
embedding_contract_id`；Agent 发布校验它与 KnowledgeBinding 完全一致，Runtime 把同一
契约传给 RAG Service，避免重建索引或更换向量模型后静默漂移。

Agent Graph 中模型只输出 `CAPABILITY`。Resolver 选定后，Tool/RAG/Agent 回到原
Tool Guard、Retrieval Guard 和 SubAgent Manager；Skill/Human/Workflow 进入有界处理器。

## 零 Agent Workflow 与状态

Workflow 可以 `topology=NONE`。每步只声明 Capability、I/O Schema、timeout、attempts
和可选 compensation capability。`ZeroAgentWorkflowRuntime` 不加载 Planner/AgentSession，
支持重试、逆序补偿、Human 挂起和原步恢复。生产长流程使用
`ZeroAgentBusinessWorkflow` Temporal Workflow。每个冻结步骤只在一个 Activity 中推进，
步骤结果先进入 History 再调度下一步，因此 Worker 重启不会重放已完成的
Provider；Signal、检查点、取消和跨区队列同样由 Temporal 持久化。

| 对象 | 所有者 | 内容 |
| --- | --- | --- |
| RootTask | Runtime/Workflow | RUNNING / WAITING / COMPLETED / FAILED / CANCELLED |
| Workflow History | Temporal | step、timer、signal、retry、compensation |
| Agent Run | Agent Runtime | 模型、工具、审批、重规划、检查点 |
| Skill Execution | SkillRuntime | CREATED / ACTIVATING / RUNNING / COMPLETED / FAILED |
| User Conversation / Memory | Context Service | 消息、摘要、用户语义状态 |
| Task Artifact | Context + Object Storage | 租户/RootTask 范围的不可变引用和 SHA-256 |

大中间结果传 `artifact_id/content_ref/content_sha256`，不把文档、检索集或报告复制到
每个状态机。

## 实现定位

| 能力 | 实现 |
| --- | --- |
| 共享契约 | `platform-sdk/platform_sdk/contracts/{skills,workflow,orchestration,artifacts}.py` |
| Registry/发布验证 | `agent-control-plane/app/{domain,application,infrastructure,api}` |
| Snapshot 冻结 | `platform-sdk/.../runtime_snapshot.py` 与 `agent-runtime/.../snapshot_compiler.py` |
| Resolver/Health/Dispatcher | `agent-runtime/.../runtime/capability_*.py` |
| Skill Runtime | `agent-runtime/.../runtime/skill_runtime.py` |
| Workflow/Temporal | `agent-runtime/.../runtime/{workflow_runtime,temporal_queue}.py` |
| Agent CAPABILITY 节点 | `agent-runtime/.../agent/{models,decision_engine,graph}.py` |
| Task Artifact Store | `rag-agent-service/app/context/artifact_store.py` |
| Skill 离线回放 | `agent-lab/app/{models,clients,service}.py` |

原设计 40 个章节的逐项代码证据和不变式见
[设计落地验收清单](skill-design-implementation-checklist.md)。
