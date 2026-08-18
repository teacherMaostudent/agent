# RAG Agent Service

通用 RAG、Context 与摄取服务。Agent Runtime 已迁出至仓库根目录的 `agent-runtime/`；
本服务不再承载 Runtime API、Agent 状态机、Planner、Harness、审批或预算状态。模型调用经
LLM Gateway，业务副作用仅经 Tool Gateway。

服务不内置行业法规、审查清单或业务工作流。租户自行摄取受 ACL 保护的企业文档，并通过
Control Plane 发布快照绑定知识源、模型、工具、预算与审批策略。

主要入口：

- `apps/agent_context_service`：固定 `History → RAG → Token Budget` Context Pipeline；会话记忆、证据排序与可解释 Token 预算。
- `apps/rag_query_api`：ACL 检索与受控文本扫描。
- `apps/ingestion_api`、`apps/ingestion_worker`：文档解析、OCR 与索引更新。

Runtime 只能经 `platform-sdk` 的 HTTP 客户端调用 Context/RAG 稳定 API，不能 import 本服务的
仓储、切块器或索引实现。拆分细节见 [Runtime 与 RAG 服务拆分说明](../docs/runtime-rag-service-split.md)。

Context Pipeline 的 Transformer 只组织“本轮模型可以看到什么”：History 按租户/用户/会话读取受限历史，
RAG 阶段按请求策略检索或标记 `memory-only` 降级，Token Budget 阶段按角色、时间、相关性和来源可信度
进行确定性选择。它不保存 Runtime Session Ledger，也不调用模型或业务工具。

## Embedding Provider 与生产 Promote

`EmbeddingProvider` 以 `EmbeddingContract` 固定 Provider、模型修订、维度、归一化、许可证、部署方式和
输入模板。`cloud_dashscope`（兼容旧 `qwen` 配置）作为 `text-embedding-v3` 的 Cloud Baseline；
`local_openai` 指向企业自部署的 OpenAI 兼容 embedding endpoint，可部署 BGE-M3 或 Qwen3-Embedding。

生产索引不允许“改配置即切模型”：先用同一 Golden Retrieval Dataset 运行 `EmbeddingBenchmarkRunner`，比较
Recall@K、False Negative、P95 延迟、估算成本、高风险召回、部署方式和许可证；通过 Governance 检索质量门禁后，
才将新的 `index_version + embedding_contract_id + retrieval_evaluation_id` 绑定到 Control Plane Release。

本地联调和生产配置统一由仓库根目录的 `compose.platform.yaml`、`compose.production.yaml` 管理。
