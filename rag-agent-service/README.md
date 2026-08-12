# RAG Agent Service

通用 RAG、Context 与摄取服务。Agent Runtime 已迁出至仓库根目录的 `agent-runtime/`；
本服务不再承载 Runtime API、Agent 状态机、Planner、Harness、审批或预算状态。模型调用经
LLM Gateway，业务副作用仅经 Tool Gateway。

服务不内置行业法规、审查清单或业务工作流。租户自行摄取受 ACL 保护的企业文档，并通过
Control Plane 发布快照绑定知识源、模型、工具、预算与审批策略。

主要入口：

- `apps/agent_context_service`：会话记忆、证据排序与 Token 预算。
- `apps/rag_query_api`：ACL 检索与受控文本扫描。
- `apps/ingestion_api`、`apps/ingestion_worker`：文档解析、OCR 与索引更新。

Runtime 只能经 `platform-sdk` 的 HTTP 客户端调用 Context/RAG 稳定 API，不能 import 本服务的
仓储、切块器或索引实现。拆分细节见 [Runtime 与 RAG 服务拆分说明](../docs/runtime-rag-service-split.md)。

本地联调和生产配置统一由仓库根目录的 `compose.platform.yaml`、`compose.production.yaml` 管理。
