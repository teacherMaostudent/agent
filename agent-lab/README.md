# Agent Lab

Agent Lab 是独立的**离线 Agent/Skill 回放编排服务**。它不训练模型、不修改草稿、
不承载线上请求，也不决定质量门禁规则。

它的职责是：冻结 Agent Snapshot 或 Active SkillVersion；为每个用例绑定精确
version/digest；调用 Runtime 的 Agent 或 Skill 公开契约；把候选输出、证据和脱敏 Ledger
提交 Governance；保存结果、失败用例和基线差异。

```text
Agent Lab
  ├─ Control Plane：解析并冻结发布快照
  ├─ Agent Runtime：仅执行已冻结快照
  ├─ Governance：Judge、校准、质量门禁
  └─ Agent Lab：回放编排、结果保存、基线比较
```

## 为什么需要独立 Agent Lab

线上 Runtime 的目标是稳定执行，不能同时承担任意实验、批量回放和候选策略比较；Governance 拥有评测规则，
也不应该自己决定要测试哪个 Agent 组合。Agent Lab 负责把一个候选 Agent 的 Prompt、Graph、RAG、Tool、
Planner、预算和权限组合变成可重复实验，并保留精确 Snapshot、输入、轨迹和结果。

它解决的是“这套 Agent 配置是否比基线更好、是否满足发布证据要求”，不是“某个基础模型是否训练成功”。

## 实验生命周期与状态

```text
DRAFT
  → prepare：解析并冻结全部用例的同一 Snapshot/Skill Digest
PREPARED
  → submit：创建唯一持久化 Job
QUEUED
  → Worker Lease
RUNNING
  → Runtime Replay + Session Ledger + Governance Judge/Gate
COMPLETED / FAILED
  → Comparison / Release Evidence / Retry / DLQ
```

API 进程只登记和提交实验，独立 Worker 才执行长时间回放。PostgreSQL 保存实验聚合、租约、尝试次数和最终
结论；Temporal 负责耐久调度。Worker 崩溃后由租约超时恢复，瞬态下游故障指数重试，超过上限进入持久化
DLQ。一个实验中的用例若解析到不同 Release/Snapshot，会在执行前拒绝，避免灰度期间产生混杂样本。

## Multi-Agent 评测

Multi-Agent 不能只比较最终答案。Agent Lab 从 Session Ledger 派生委派次数、专家选择、父子调用深度、工具
调用、模型调用、审批、恢复、权限违规、Evidence Recall、工具选择 Precision、成本和延迟。测试集可以分别
覆盖主管拆解、专家能力、冲突收敛、预算切分和故障恢复。

推荐将简单单 Agent 或直接 ReAct 设为 baseline，再比较主管—专家、平级 Quorum 或动态 Capability Routing。
Judge 衡量答案质量，确定性轨迹指标衡量 Harness 和组织行为，两者不能相互替代。

## 与 Model Lab 的边界

| 服务 | 研究对象 | 可作为发布依据的产物 |
| --- | --- | --- |
| Model Lab | 模型、微调、量化、训练 | 模型卡、模型工件、训练评测 |
| Agent Lab | Prompt、Graph、Tool、RAG、Planner、预算和权限组合 | 回放结果、失败 Trace、基线差异、Governance Gate |

## API

- `POST /v1/experiments`：登记不可变回放计划；
- `POST /v1/experiments/{id}/prepare`：解析并冻结每个用例的已发布快照；
- `POST /v1/experiments/{id}/run`：冻结后创建持久化任务，返回 `202` 与 `job_id`；
- `GET /v1/jobs/{job_id}`：读取任务的租约、重试次数和 DLQ 状态；
- `GET /v1/experiments/{id}`：读取实验及用例结果；
- `GET /v1/experiments/{id}/comparison`：与基线实验比较。
- `GET /internal/v1/experiments/{id}/release-evidence`：只向 Control Plane 返回通过门禁的不可变证据。

## Harness Benchmark

每个 `ReplayCase` 可固定 `expected_evidence_ids` 与 `expected_tool_names`。Agent Lab 从
Runtime 的脱敏 Session Ledger 确定性计算任务成功率、工具/模型调用数、检索轮次、人工
审批率、恢复事件、权限违规、Evidence Recall 与工具选择 Precision；Comparison 同时给出
平均延迟和已知 USD 成本。推荐把“直接 ReAct”实验设为 baseline，再分别比较受控
Plan-Execute、Context 压缩和失败恢复策略。Judge 负责回答质量，轨迹指标负责 Harness
行为，两者不能相互替代。

## 受限 Sandbox Case

`ExperimentPlan.sandbox_cases` 可为工具适配器、解析器或确定性代码声明独立验证。每条用例只接收
`image + argv + timeout + expected_exit_code`，禁止 Shell 字符串、宿主机挂载和网络访问；镜像必须精确命中
`AGENT_LAB_SANDBOX_IMAGE_ALLOWLIST`。结果只保存镜像、命令 SHA-256、Provider、退出码和 stdout/stderr 的
长度与 SHA-256，不保存原始输出，以免把工具输出的秘密或业务数据写进实验记录。

Docker Provider 仅适合一台**独立、一次性实验 Worker 主机**上的本地/预生产验证：它以只读根文件系统、
无网络、无 Linux capability、非 root UID、PID/CPU/内存/tmpfs 限制运行容器。不要把 Docker Socket 挂进
Agent Lab API 或普通 Worker 容器；拥有该 Socket 等价于拥有宿主机控制权。生产应将 `SandboxProvider` 接到
单独的 MicroVM/Kata/Firecracker 执行池，并把该池放在不含业务数据库凭证的网络隔离节点。当前
`microvm` Provider 是明确的部署扩展点；没有安装外部执行器时会失败关闭，不会悄悄回退到宿主机执行。

本地模式使用 SQLite 和同步本地队列，只用于开发、教学与契约测试。生产模式启动时强制要求
PostgreSQL、Temporal、OIDC、工作负载令牌与 mTLS。API 只创建并提交任务；独立 Temporal Worker 通过
数据库租约领取任务，避免长时间回放占用 Web 进程，也避免 Worker 故障后出现双重结算。

## 发布接线

Agent Lab 的 `release-evidence` 内部接口只会返回已完成、快照已冻结且 Governance Gate 通过的实验。Control Plane 在启用 `CONTROL_PLANE_AGENT_LAB_REQUIRED=true` 后，会强制校验：实验租户、Agent、版本、实验环境为 `laboratory`，并且 Release 请求携带的 `quality_gate_run_id` 必须与该实验的 Judge Run 完全一致。这样实验不能被替换成无关 Agent、草稿或人工伪造的 Gate。

## 运行与安全边界

本地可使用以下方式启动：

```powershell
cd C:\Users\Administrator\Documents\AI工作\agent\agent-lab
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
Copy-Item .env.example .env
uvicorn app.main:app --reload --port 8092
```

`/internal/v1/experiments/{id}/release-evidence` 不是面向终端用户的 API，仅供 Control Plane
使用。生产通过 OIDC 工作负载身份与 mTLS 认证，`AGENT_LAB_SERVICE_API_KEY` 仅保留为迁移期的纵深
校验；它必须与 `CONTROL_PLANE_AGENT_LAB_SERVICE_API_KEY` 一致，且不得暴露到浏览器、日志或普通业务调用方。

API 的 `prepare` 阶段只解析并冻结快照，`run` 阶段只提交任务。每个用例均使用稳定的实验
Session ID，Worker 也使用稳定的 Runtime `request_id`，因此既能检测一个实验中不同用例被解析到不同
Release 或 snapshot hash 的漂移，也能令网络重试落入 Runtime 的幂等边界。

回放完成后，Agent Lab 会读取同一 Session 的脱敏 Event Ledger，并保存事件数量与最终序号。这使同一
Golden Case 的比较不再只看最终答案，也能定位 Prompt、模型、工具或上下文选择在哪个 Step 发生差异。
Ledger 读取失败不会把已经完成的 Runtime 执行篡改为失败实验，但结果会缺少该解释材料。

## 当前实现与生产演进

生产配置已经实现 PostgreSQL Repository、Temporal Worker、`FOR UPDATE SKIP LOCKED` 任务租约、指数
退避、持久化 DLQ、OIDC/mTLS 和 API/Worker 分工作负载。实验聚合与任务记录分表：Temporal 只负责
调度，数据库才是任务状态和最终结论的真源。仍需要部署侧配置网络策略、可观测性告警与数据保留策略；
详见[仓库部署指南](../docs/deployment-guide.md)。
