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

## 迁移阶段

1. **当前阶段：物理迁出、单实现。** `agent-runtime/src/agent_runtime_service` 持有 Agent、Planner、Harness、预算、审批、运行状态、Runtime API 与 Temporal Worker。RAG 镜像只提供共享契约/Context 客户端及知识服务实现，确保没有复制出的第二套状态机。
2. **双运行验证。** 老入口 `apps.agent_runtime.main` 仍保留，供现有测试、回滚和集成方使用；新镜像的入口 `agent_runtime_service.main` 复用同一 ASGI 应用。两者处理相同请求时，应产生相同的发布快照、计划、状态和幂等结果。
3. **流量切换。** 通过网关/部署清单将 Runtime 流量和 Temporal Worker 切到 `agent-runtime` 镜像；RAG Query、摄取 Worker 保持使用 `rag-agent-service` 镜像。此时不允许一个 Pod 同时承载 Runtime 与 RAG API。
4. **物理迁包。** 当所有调用方、部署脚本和回滚流程均稳定后，将 `app/agent`、`app/runtime` 及其专属 API/Worker 物理迁入 `agent-runtime`，共享 Contracts/身份/可观测性模块改为独立共享包。完成前不得删除兼容入口。

## 非目标与安全边界

- Context Service 继续负责历史消息的组织、排序和 Token Budget；Runtime 通过 HTTP 调用它获取记忆。
- Runtime 通过 HTTP 直接调用 RAG 获取证据；Context 不再是 Runtime 到 RAG 的隐式依赖链。
- Tool Gateway 仍是唯一的业务副作用执行入口；RAG 的 `controlled_scan` 只返回已脱敏、限量的观察结果。
- 本次不改变 Agent 发布快照、审批、预算、Outbox 或 Temporal 的状态模型，因此运行中的任务可按既有幂等和恢复语义继续执行。
