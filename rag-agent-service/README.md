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
  → Query understanding / deterministic rewrite (profile-bounded)
  → Query Embedding
  → RAG Query API
  → Dense provider path (OpenSearch default; optional Milvus *or* pgvector provider)
  → BM25 lexical path (OpenSearch)
  → Independent Candidate recall
  → RRF Fusion + deterministic deduplication
  → Optional versioned Reranker
  → Evidence Verifier (ACL / index version / source / freshness / integrity / injection)
  → Evidence Schema + Provenance + Scores
  → Context Projection
```

检索命中首先是 `RetrievalCandidate`，不是 Evidence。候选必须在 Query Plane 经二次 ACL、知识状态、
有效期、可选内容摘要和提示注入复核后才会变成 `Evidence`，并被允许投影给 Context/LLM。每条 Evidence
携带 Index、Embedding Contract、Retrieval Profile、Reranker Revision 与来源血缘；Runtime 与 Context
仍会执行分段、脱敏、Token 限制和 Prompt 组装。

### 受控检索策略

发布快照冻结 `RetrievalProfilePolicy` 的默认档位、允许档位和 revision。Runtime 只能在允许集合中选择
`FAST`、`STANDARD`、`STRICT_EVIDENCE`、`DEEP_RESEARCH` 或 `NO_RAG`；RAG 在服务端再次解析并硬执行
候选数、Evidence 数与重排开关，旧客户端传入的 `top_k` 不得扩大档位边界。这样可避免 Runtime 或调用方
绕过发布策略，同时保留每次选择的 Trace 血缘。

高风险、需审计的发布可选择 `ENTERPRISE_EVIDENCE`：最多召回 100 个 Candidate，经固定 Revision 的
Cross-Encoder/Reranker 后，最多投影 10 条已验证 Evidence。`STANDARD` 则保留较小候选池，避免普通知识问答
无条件增加向量、精排、成本与延迟。Provider 的选择和 Reranker Contract 都由 Knowledge Binding/Snapshot 冻结，
不是由 Runtime 请求参数决定。

## 检索 Provider 与部署选型

`RetrievalProvider` 使用能力声明而非绑定某个数据库 SDK。当前默认生产实现为 OpenSearch：它同时承担
词法、Dense、租户/ACL、状态与版本过滤，并以 HNSW 建立版本化向量索引。Milvus、pgvector 与 Graph
不是默认 Compose 依赖：只有容量、QPS 或图关系推理的压测/业务证据证明 OpenSearch 不足时，才新增对应
Provider、Index Build Manifest、对账和 Retrieval Release；不能把多个数据库的双写复杂度伪装成“可插拔”。

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
  → Index Build Manifest (chunk/document set digest + reconciliation)
  → Retrieval Evaluation
  → Control Plane Knowledge Binding
```

摄取 API 使用稳定文档 ID、任务 ID 和内容摘要实现幂等。Desktop 扫描结果不能直接污染知识库：只有经过审批的
Artifact 才能提交，并保存审批人、审批 ID、Root Task 和 Artifact ID 作为来源元数据。对象存储保存原始文档，
索引保存派生 Chunk；OCR 文本是带来源标记的派生证据，原始文件仍是权威工件。

生产索引使用不可变 `index_version`。重建新索引不会原地覆盖旧索引；Control Plane 只有在检索评测通过后才把
新版本绑定到 Release，因此 Runtime 可以用 Snapshot 还原一次回答实际使用的索引空间。

每次摄取或显式重建都会生成 `IndexBuildManifest`：其中固定 Tenant、索引版本、Embedding Contract、切块修订、
文档/Chunk 集合摘要、计数和对账结果。Runtime 请求携带的 Manifest 必须处于 `READY`、属于同一 Tenant 且与当前
索引版本一致；否则 RAG 在检索前拒绝，而不是从可能半完成的别名继续返回证据。检索方案不维护第二套独立的
“Retrieval Release”状态机：Manifest、Reranker 和 Retrieval Policy 已随 Agent Snapshot 冻结，直接复用
Control Plane 的 Shadow → Canary → Promote/Rollback 生命周期，避免两套发布真相漂移。

`REINDEX_KNOWLEDGE_BASE` 是全库重建任务：它只从 PostgreSQL/对象存储所代表的权威文档集合重放，排除
`revoked`、`quarantined` 和 `untrusted` 来源，随后生成全库 Document/Chunk 摘要与对账 Manifest。Worker 无权
自行切换 OpenSearch Alias；评测、Snapshot 绑定和部署激活仍必须由 Control Plane 的发布流程完成。

上游源系统可使用 `POST /ingestion/sources/{source_id}/status` 投递撤销、隔离或恢复事件，且必须持有
`rag:source:revoke`。服务先更新权威 Document 元数据，再以 Tenant + source_id 更新索引投影；原件不删除，
Evidence Verifier 因 `source_status` 停止投影，保留审计、复核与重摄取依据。

### 权限分区的语义缓存

`RAG_SEMANTIC_CACHE_BACKEND=redis` 可启用短 TTL 的语义响应缓存。它位于 Evidence Verifier 之后，只保存已经
验证的响应；缓存桶同时绑定 Tenant、User、授权范围摘要、Document、Index Manifest、Embedding Contract、
Retrieval Profile 和 Reranker Contract。缺失授权摘要或 Manifest、包含临时正文的请求一律 `BYPASS`，因此缓存
不能跨用户、权限变更或索引工件复用。Runtime 会把 `BYPASS` / `MISS` / `HIT_SEMANTIC` 写入 retrieval observation。
生产只允许 `redis` 或 `disabled`，本地 `memory` 后端只用于开发验证；Redis 故障自动退化为 cache miss，不影响
权威检索与 ACL 校验。

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
| `GET` | `/ingestion/index-manifests/{manifest_id}` | 查询冻结的索引构建清单 |

实际前缀以各工作负载的 `RAG_API_PREFIX` 为准，OpenAPI 是接口字段的权威来源。

## 本地启动与验证

七服务联调使用仓库根目录：

```powershell
.\scripts\start-platform.ps1 -Action Start
py -3.12 scripts\platform_e2e.py
```

要验证真实的 OpenSearch 版本化投影，而非默认本地检索实现，显式启用检索 profile：

```powershell
.\scripts\start-platform.ps1 -Action Start -WithEnterpriseRetrieval
py -3.12 scripts\local_scenarios.py
```

这会启动本机单节点 OpenSearch（宿主端口 `9200`），并验证“上传 → 摄取 Worker → 向量/BM25
召回 → ACL/Evidence”路径。该 profile 为开发联调而关闭 OpenSearch 安全插件；生产只能使用
`compose.production.yaml` 的受管集群、独立凭据和网络边界。

服务级测试：

```powershell
cd rag-agent-service
py -3.12 -m pytest -q
```

生产模板启动检查会强制 PostgreSQL、Temporal、对象存储、OpenSearch、语义 Embedding Provider、OIDC、
mTLS、OPA、Redis 与 CDC 模式。Compose 是部署模板，不代表已经完成多区域 HA、容量或恢复演练。

Runtime、Context 与 RAG 的最终物理边界和平台全链路见
[架构总览](../docs/architecture-overview.md)。
