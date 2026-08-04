# RAG Agent Service

通用 RAG、上下文与 Agent Runtime 服务。在线组件分为 Runtime、Context、RAG Query 与
Ingestion Worker；模型调用经 LLM Gateway，工具副作用经 Tool Gateway。

服务不内置行业法规、审查清单或业务工作流。租户自行摄取受 ACL 保护的企业文档，并通过
Control Plane 发布快照绑定知识源、模型、工具、预算与审批策略。

主要入口：

- `apps/agent_runtime`：执行已发布 Agent 快照与 Harness。
- `apps/agent_context_service`：会话记忆、证据排序与 Token 预算。
- `apps/rag_query_api`：ACL 检索与受控文本扫描。
- `apps/ingestion_api`、`apps/ingestion_worker`：文档解析、OCR 与索引更新。

本地联调使用 `compose.services.yml`；生产配置由仓库根目录的
`compose.production.yaml` 提供。
