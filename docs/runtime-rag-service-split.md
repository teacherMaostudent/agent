# Runtime 与 RAG 服务拆分迁移说明

## 目标边界

`agent-runtime` 是独立的执行平面。它负责加载 Control Plane 发布快照、运行 Planner、Harness 和 LangGraph 状态机、管理预算/审批/取消、提交 Temporal 工作流，并交付一笔业务任务的最终结果。

`rag-query-api` 和 `ingestion-api` 是独立的知识平面。它们负责摄取文档、构建和切换索引版本、ACL 过滤、检索、受控文本扫描及健康检查；不参与 Agent 的下一步业务决策。

## 稳定 RAG 契约

Runtime 不得导入 RAG 的仓储、检索器、切块器或索引实现。它只使用以下 HTTP API：

| 能力 | API | 用途 |
| --- | --- | --- |
| 检索 | `POST /api/v1/query/search` | 返回已 ACL 过滤、可引用的证据 |
| 受控扫描 | `POST /api/v1/query/scan` | 在 RAG 策略限定的文本范围内扫描 |
| 能力发现 | `GET /api/v1/query/capabilities` | 部署/兼容检查，不暴露内部实现 |
| 索引版本 | `GET /api/v1/query/index-version` | 发布与运行时确认所使用的知识版本 |
| 摄取 | `/api/v1/ingestion/*` | 上传、创建和查询摄取作业 |
| 健康检查 | `/api/v1/health`、`/api/v1/health/ready` | 负载均衡及发布前就绪验证 |

`RagSearchResponse.index_version` 与 `/index-version` 都是公开契约的一部分。后续 Control Plane 可在发布时校验 Agent 绑定的知识索引版本是否存在，Runtime 可在执行前拒绝知识版本漂移。

## 当前完成状态

拆分已完成，线上状态机只有一份：`agent-runtime/src/agent_runtime_service` 持有 Agent、Planner、
Harness、预算、审批、运行状态、Runtime API 与 Temporal Worker。RAG 服务只保留 Context、RAG
Query、摄取 API/Worker 和索引实现；不存在可接收线上 Runtime 流量的旧入口。

Runtime、Context、RAG 与摄取工作负载只共同依赖 `platform-sdk`、`platform-infra`，不互相安装或
import 应用包。部署清单为 Runtime、Context、RAG Query、摄取 API 与摄取 Worker 分配独立镜像、
OIDC 工作负载身份和 mTLS 证书；它们不应合并到同一 Pod。

## 非目标与安全边界

- Context Service 继续负责历史消息的组织、排序和 Token Budget；Runtime 通过 HTTP 调用它获取记忆。
- Runtime 通过 HTTP 直接调用 RAG 获取证据；Context 不再是 Runtime 到 RAG 的隐式依赖链。
- Tool Gateway 仍是唯一的业务副作用执行入口；RAG 的 `controlled_scan` 只返回已脱敏、限量的观察结果。
- 本次不改变 Agent 发布快照、审批、预算、Outbox 或 Temporal 的状态模型，因此运行中的任务可按既有幂等和恢复语义继续执行。
