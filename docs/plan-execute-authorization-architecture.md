# Plan-Execute 与分层授权执行架构

## 最终抽象

平台不把 Plan-Execute、ReAct、Managed Context、LangGraph、Deep Agents 和 Temporal
当成互斥模式。发布快照分别冻结以下正交维度：

| 维度 | 契约 | 职责 |
| --- | --- | --- |
| Planning Strategy | `PLAN_EXECUTE` | 形成宏观计划，逐步执行并受控重规划 |
| Step Strategy | `DETERMINISTIC` / `REACT` | 固定步骤直接执行；不确定局部步骤才进入有界 ReAct |
| Execution Engine | `SIMPLE` / `LANGGRAPH` / `DEEP_AGENTS` | 解释任务步骤，不决定持久化方式 |
| Durability | `EPHEMERAL` / `CHECKPOINTED` / `TEMPORAL` | 保存、恢复和重放执行历史 |
| Context Strategy | `MANAGED_LEDGER` | 分开管理事实、证据、步骤、失败尝试、产物和决策 |
| Tool Presentation | `NATIVE` | 只向模型呈现发布快照允许的工具 Schema |
| Trust/Governance | 版本化 Policy | 计划准入、动作授权、审计、评测与发布门禁 |

旧 `lifecycle/reasoning/runtime_executor` 仍可读取，但只作为迁移投影。新代码按
`engine + durability` 解析部署组合。Temporal 是 Durability Adapter，不是第四种
Agent Executor。默认 Runtime 当前部署 Simple 与 LangGraph；`DEEP_AGENTS` 只是受支持的
SPI 契约，只有集群实际注册 `deep-agents/v1` 后 Control Plane 才允许发布，旧
`agentic/v1` 不会被冒充为 Deep Agents。

## 执行与授权链路

```text
Request
  → Context Assembly
  → Planner
  → ProposedExecutionPlan
  → PlanAdmissionService / Runtime Guard
  → AdmittedExecutionPlan
  → Execution Engine
  → Proposed Action
  → Tool Gateway Step Authorization
  → Approval / Idempotency Claim / Commit
  → Observation
  → Verifier
  → Pass / Retry / Replan
```

`Plan Admission` 只确认计划结构、Executor、能力可行性、工具范围、风险声明、预算和
截止时间可以进入执行阶段。它不会提前批准退款、写库或删除对象。准入结果包含
`admission_id` 和逐项检查事实。

每次真实工具动作关联 `plan_id`、`plan_admission_id`、`step_id`、`operation_id` 和
`tool_execution_id`。Tool Gateway 是最终动作授权与副作用出口。生产 Runtime 的写操作
缺少这些身份时失败关闭；审批绑定到精确请求哈希且只能消费一次。Gateway 分别发布
`tool.authorization.decided` 与 `tool.execution.completed`，使提出、授权、提交和结果
能够独立审计。

## Skill 治理

Skill 采用 Core Contract 加可选 Governance Profile：

- `PURE`：无工具副作用；
- `READ_ONLY`：只允许只读 Tool；
- `REVERSIBLE_WRITE`：必须绑定补偿 Skill；
- `HIGH_RISK_WRITE`：必须绑定确定性 Verifier；
- `HUMAN_APPROVAL_REQUIRED`：写工具必须由 Tool Catalog 声明审批。

Tool 风险和权限的最终事实源仍是 Tool Catalog。Control Plane 发布 Agent 时把 Catalog
中的风险、审批、幂等和权限冻结到 Snapshot；发布 Skill 时校验 Skill Profile 没有降低
真实工具风险。Runtime 只执行受支持的前置、后置和 Verifier ID，未知规则不会被接受后
忽略。业务 API 仍负责对象级权限和事务约束。

## 在线与离线边界

| 组件 | 在线职责 | 离线职责 |
| --- | --- | --- |
| Runtime Guard | 计划准入、预算、截止时间、能力和工具范围 | 无 |
| Tool Gateway | 单步策略、审批、幂等、提交和对账 | 目录版本维护不在请求链路中完成 |
| Governance | 异步接收运行事实与合规检查 | Golden、Judge、回归、Bad Case、质量门禁 |
| Agent Lab | 无线上用户流量 | 冻结快照、离线回放、基线比较、请求 Governance 评测 |
| Control Plane | 解析正式 Release | 定义、编译、发布以及消费门禁证据 |

Governance 定义和评估规则，但不成为每次 Tool 调用的同步最终授权服务。Runtime 使用
发布快照做计划级控制，Tool Gateway 使用当前可信身份和最新动作参数做最终动作级控制，
避免形成两个相互冲突的授权事实源。

## Harness 边界

Harness 仍只承担七项生命周期操作：解析 Release、加载 Snapshot、创建 ExecutionContext、
解析 Executor、运行、恢复和取消。Planner、Plan Admission、Context、Prompt Assembly、
LLM Routing、Tool Authorization 和领域规则均不进入 Harness。
