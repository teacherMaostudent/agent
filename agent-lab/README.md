# Agent Lab

Agent Lab 是独立的**离线 Agent 回放编排服务**。它不训练模型、不修改 Agent 草稿、不承载线上请求，也不决定质量门禁规则。

它的职责是：固定一次由 Control Plane 解析得到的已发布快照；为每个回放用例建立稳定 Session 绑定；调用 Agent Runtime；把候选答案和证据提交给 Governance；保存结果、失败用例和基线差异。

```text
Agent Lab
  ├─ Control Plane：解析并冻结发布快照
  ├─ Agent Runtime：仅执行已冻结快照
  ├─ Governance：Judge、校准、质量门禁
  └─ Agent Lab：回放编排、结果保存、基线比较
```

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

## 当前实现与生产演进

生产配置已经实现 PostgreSQL Repository、Temporal Worker、`FOR UPDATE SKIP LOCKED` 任务租约、指数
退避、持久化 DLQ、OIDC/mTLS 和 API/Worker 分工作负载。实验聚合与任务记录分表：Temporal 只负责
调度，数据库才是任务状态和最终结论的真源。仍需要部署侧配置网络策略、可观测性告警与数据保留策略；
详见[仓库部署指南](../docs/deployment-guide.md)。
