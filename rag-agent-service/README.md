# RAG Service 与知识摄取

本工程承载企业知识的在线检索与离线摄取，同时包含独立部署的 Context Service 代码。Agent Runtime 已物理
迁移到仓库根目录的 `agent-runtime/`；这里不再拥有 Planner、Harness、Agent Loop、审批、预算或运行状态。

RAG 的职责是回答“在当前身份、索引版本和检索策略下，可以提供哪些知识证据”，而不是判断 Agent 下一步
应该检索、调用工具还是回答。它不内置行业规则，业务差异通过租户文档、ACL、索引版本和 Control Plane
发布绑定表达。

## 为什么需要独立 RAG 服务

把检索直接写进 Runtime 会导致 Agent 编排代码掌握文档存储、索引管理和 ACL 细节，也会让检索扩容、索引
重建和数据保留影响在线状态机。本工程把在线查询、离线摄取和 Context 组装拆成独立工作负载：

| 工作负载 | 职责 | 明确不负责 |
| --- | --- | --- |
| `rag-query-api` | ACL 检索、混合召回、重排、Evidence 返回、索引能力声明 | Agent 决策和业务副作用 |
| `ingestion-api` | 接收文档/Artifact 引用并创建幂等摄取任务 | 在 HTTP 请求中同步解析大文档 |
| `ingestion-worker` | 解析、OCR、切块、Embedding、构建索引版本 | 正式发布 Agent |
| `ingestion-temporal-worker` | 长任务重试、恢复和耐久调度 | 取代数据库任务真源 |
| `agent-context-service` | 历史消息、证据排序和 Token Budget | 保存 Runtime Session Ledger |

Context Service 的详细说明见 [Context Service README](apps/agent_context_service/README.md)。

## 在线检索链路

```text
Runtime / Context
  → Verified Identity + Knowledge Binding + Retrieval Policy
  → RAG Query API
  → Tenant / User / Document ACL
  → Immutable Index Version
  → BM25 / Vector / Hybrid Retrieval
  → Optional Reranker
  → Evidence Schema + Provenance + Scores
  → Context Projection
```

查询结果是带文档、Chunk、来源、版本、分数和权限语义的 Evidence，不是可直接信任的 Prompt 文本。Runtime
与 Context 在进入模型前还会执行分段、脱敏、注入检测、长度限制和证据验证。

### 受控检索策略

发布快照可以选择预定义检索档位，而不是让模型自由拼装任意后端查询。策略可约束查询扩写、关键词/向量权重、
候选数量、重排、最大证据数、最低得分和成本上限。Runtime 决定何时请求检索，RAG 只执行被允许的策略并返回
可解释事实。

### Controlled Scan

`POST /query/scan` 只扫描管理员预注册的日志、源码或文本目录。它限制 Scope、Glob、正则复杂度、命中数量和
正文长度，并在返回前脱敏。无效模型参数返回稳定 422，后端不可用返回 503；二者不会被伪装成未知 500 反复
消耗 Agent 尝试预算。生产工具调用仍应通过 Tool Gateway 的 `controlled_scan` 目录、权限和审计边界。

## 离线摄取与索引生命周期

```text
Document / Approved Task Artifact
  → Ingestion API
  → Durable Job
  → Parse / OCR / Normalize
  → Chunk + Metadata + ACL
  → Embedding Provider
  → Immutable Index Version
  → Retrieval Evaluation
  → Control Plane Knowledge Binding
```

摄取 API 使用稳定文档 ID、任务 ID 和内容摘要实现幂等。Desktop 扫描结果不能直接污染知识库：只有经过审批的
Artifact 才能提交，并保存审批人、审批 ID、Root Task 和 Artifact ID 作为来源元数据。对象存储保存原始文档，
索引保存派生 Chunk；OCR 文本是带来源标记的派生证据，原始文件仍是权威工件。

生产索引使用不可变 `index_version`。重建新索引不会原地覆盖旧索引；Control Plane 只有在检索评测通过后才把
新版本绑定到 Release，因此 Runtime 可以用 Snapshot 还原一次回答实际使用的索引空间。

## Embedding Provider 与向量空间一致性

`EmbeddingProvider` 通过 `EmbeddingContract` 固定 Provider、模型 revision、维度、归一化、输入模板、许可证
和部署方式。当前支持：

- `cloud_dashscope`：`text-embedding-v3` Cloud Baseline；
- `local_openai`：兼容 OpenAI Embedding API 的企业自部署端点，可接 BGE-M3 或 Qwen3-Embedding；
- deterministic hash：仅供本地和契约测试，生产启动检查禁止使用。

更换 Embedding 不是修改一个模型名，而是创建新 Contract 和新索引。`EmbeddingBenchmarkRunner` 使用同一
Golden Retrieval Dataset 比较 Recall@K、False Negative、高风险召回、P95 时延、成本和合规属性；查询端、
摄取端和发布快照的 Contract/维度不一致时必须拒绝。

## Multi-Agent 中的作用

每个专家 Agent 使用自己的 Snapshot 和 Knowledge Binding。主管 Agent 不会因为委派专家而自动获得专家的
文档权限，专家结果只以带 Evidence ID、索引版本和来源的 `AgentResult` 返回。多个专家引用冲突文档时，上层
可以按来源权威、时效性、Quorum、Judge 或人工审查收敛，而不是让 RAG 擅自决定业务结论。

RAG 可独立扩展查询副本和索引分片；大量 Agent 共享基础检索设施，但通过 Tenant、ACL、索引版本和策略隔离
各自数据域。

## 数据、安全和一致性边界

- 生产使用 OIDC/JWT、工作负载身份和 mTLS，不信任外部调用方自行设置的身份 Header；
- OPA 和索引内 ACL 共同限制 Tenant/User/Document 范围，业务源系统仍负责源数据对象级权限；
- PostgreSQL 是摄取任务和元数据真源，Temporal 负责调度，不能替代业务状态；
- OpenSearch/向量数据库保存版本化索引，Redis 只作缓存；
- 对象存储保存原文和不可变 Artifact，生产启用 KMS、版本和保留策略；
- Outbox 与 CDC/Kafka 把摄取和索引事件可靠投递给 Governance；
- 可选 RAG 失败只能形成显式 `memory-only` 降级，必需 RAG 失败必须向上返回。

## 主要 API

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `GET` | `/query/capabilities` | 查询部署支持的检索能力 |
| `GET` | `/query/index-version` | 获取当前索引与 Embedding Contract |
| `POST` | `/query/search` | 在 ACL 和策略约束下检索 Evidence |
| `POST` | `/query/scan` | 执行受控文本扫描 |
| `POST` | `/ingestion/documents` | 提交文档摄取 |
| `POST` | `/ingestion/artifacts` | 提交已审批 Artifact |
| `POST/GET` | `/ingestion/jobs` | 创建或查询摄取任务 |
| `GET` | `/ingestion/documents/{document_id}` | 查询文档元数据 |

实际前缀以各工作负载的 `RAG_API_PREFIX` 为准，OpenAPI 是接口字段的权威来源。

## 本地启动与验证

七服务联调使用仓库根目录：

```powershell
docker compose -f compose.platform.yaml up --build -d
python scripts/platform_e2e.py
```

服务级测试：

```powershell
cd rag-agent-service
py -3.12 -m pytest -q
```

生产模板启动检查会强制 PostgreSQL、Temporal、对象存储、OpenSearch、语义 Embedding Provider、OIDC、
mTLS、OPA、Redis 与 CDC 模式。Compose 是部署模板，不代表已经完成多区域 HA、容量或恢复演练。

Runtime、Context 与 RAG 的最终物理边界和平台全链路见
[架构总览](../docs/architecture-overview.md)。
