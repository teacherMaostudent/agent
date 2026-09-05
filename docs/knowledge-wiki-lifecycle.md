# 受治理的 Living Wiki 生命周期

## 定位

Knowledge Wiki 是 Agent Platform 的可选知识演化域，不是新的 Agent Runtime，也不替代 RAG。它解决的
问题是：一次任务中的 Evidence、模型结论和专家审查，如何在保留证据等级与责任人的前提下，成为可版本化、
可撤回、可评测的组织知识。

```text
Runtime Evidence + Task conclusion + Review result
                         │ compile
                         ▼
              WikiCandidate / pending_review
                         │ expert approve/reject
                         ▼
          WikiPage(human_confirmed) + Relations
                         │ same DB transaction
                         ▼
                Transactional Outbox
              ┌──────────┴──────────┐
              ▼                     ▼
       RAG Wiki ingestion      Governance audit
       PARSE + index ACL       Knowledge Change Gate
              │                     │
              └──────────┬──────────┘
                         ▼
            Golden Retrieval regression
                         │
              pass → release evidence
              fail → hold / supersede / rollback
```

## 知识等级与发布规则

| 等级 | 权威含义 | 创建者 | 可直接用于组织检索 |
|---|---|---|---|
| `raw_evidence` | 原始文档、工具 Observation 或 Artifact 的引用与摘要 | Runtime/RAG/Tool | 否 |
| `model_inference` | 模型对证据的归纳、规则候选或任务结论 | 固定编译器版本 | 否 |
| `human_confirmed` | 专家对候选正文、来源与有效期的明确批准 | `knowledge-reviewer` | 是，但受回归门禁约束 |

编译请求必须至少包含一个 `raw_evidence`。人工批准只新增一个 `human_confirmed` 审查来源，不会把原始
证据或模型归纳改写成人工事实。页面保存编译模型、Prompt 版本、候选 ID、来源摘要、审查人和意见。

## 页面与关系

页面支持 entity、concept、rule、procedure、decision。标题规范化后形成 `canonical_key`；相同主题内容
发生变化会产生 `conflicts_with`，不会静默覆盖。共享治理标签生成 `links_to`；专家显式选择旧页后才生成
`supersedes`。页面版本不可变，新结论只能创建新版本。

`valid_until` 到期与 `supersedes` 都会进入 RAG 索引生命周期字段。查询在 ACL 过滤阶段同时排除过期或
被替代页面，避免旧知识参与召回得分。原始页面仍保留在 PostgreSQL，便于审计和重建历史索引。

## 事务与下游闭环

批准事务原子保存页面、关系和四种 Outbox 事件：

1. `wiki.page.published`：不可变发布事实；
2. `wiki.rag.reindex.requested`：包含批准后的 Markdown、摘要和来源，进入幂等 PARSE 作业；
3. `wiki.evaluation.requested`：记录需要回归的知识变更；
4. `wiki.release_gate.requested`：在 Governance 创建待完成的知识变更门禁。

Relay 使用租约避免多副本同时消费，失败后指数退避，耗尽后进入 DLQ。摄取端按租户、页面 ID 与版本
生成稳定 Document/Job ID，并核验正文 SHA-256。Governance 只创建 `PENDING_EVALUATION`，不会在没有
Golden Dataset 结果时伪造通过；正式门禁至少需要 Recall@K、Precision@K、MRR、nDCG 与 Faithfulness。

## 安全边界

- 所有读取和写入均带 tenant；跨租户页面、supersede 目标和索引更新被拒绝或隐藏。
- 模型不能调用批准接口；批准需要 `knowledge-reviewer`，生产入口同时要求 OIDC 与服务凭证。
- Wiki 服务不直接写 OpenSearch，不同步调用业务事务，也不执行 Agent 或工具。
- RAG 文档存储是摄取事实来源，OpenSearch 是可重建投影；Governance 是门禁事实来源。

## 验收场景

1. 只有模型归纳、没有原始 Evidence 的候选返回 422。
2. 普通调用者批准候选返回 403；专家批准生成 human-confirmed 页面和四条 Outbox。
3. 同一候选重复审批返回 409；同一 Wiki 版本重复摄取返回相同 Document/Job。
4. 同一主题不同内容产生 conflict；显式替代产生 supersede，旧页不再被 RAG 召回。
5. 到期页面不再被 RAG 召回，但仍可从权威 Wiki 历史中审计。
6. 下游故障不会回滚已批准页面；投递重试耗尽进入 DLQ，门禁保持未通过。
7. Wiki 变更只创建待评测门禁；没有真实检索指标时不得进入正式发布证据。
