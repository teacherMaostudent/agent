# Knowledge Wiki Service

Knowledge Wiki Service 是企业 Agent Platform 的可选知识演化服务。它把 Runtime Evidence、任务结论、
Artifact 和人工审查结果编译为版本化 Wiki，但不会替代 RAG、Context 或 Governance。

## 责任边界

```text
Evidence / Run conclusion / Review
                 │ compile
                 ▼
        WikiCandidate(PENDING_REVIEW)
                 │ knowledge-reviewer approve
                 ▼
 WikiPage + Relations + Transactional Outbox
       ├── RAG REINDEX job
       ├── Governance audit event
       └── Knowledge Change Gate(PENDING_EVALUATION)
```

服务负责实体、概念、规则、流程和决策页；维护 `links_to`、`conflicts_with`、`supersedes`、来源、
版本和有效期。它不允许模型直接发布组织知识，不把模拟评测当作门禁通过，也不在审批事务内同步调用
RAG 或 Governance。

## 知识等级

| 等级 | 含义 | 是否可直接成为组织知识 |
|---|---|---|
| `raw_evidence` | 原始 Evidence/Artifact 的哈希和引用 | 否，必须保留来源 |
| `model_inference` | 模型归纳或任务结论 | 否，属于候选声明 |
| `human_confirmed` | 专家批准后的 Wiki 页版本 | 是，但仍受有效期和回归门禁约束 |

每个候选至少包含一个 `raw_evidence`。批准后的页面仍保存全部来源等级、内容哈希、编译模型、Prompt
版本、审查人和审查意见，因此“人工确认”不会抹掉它来自模型归纳的事实。

## API

- `POST /v1/wiki/candidates`：登记候选知识，不发布。
- `GET /v1/wiki/candidates`：按租户和状态列出候选，供专家审查队列分页读取。
- `GET /v1/wiki/candidates/{candidate_id}`：读取当前租户候选。
- `POST /v1/wiki/candidates/{candidate_id}/review`：仅 `knowledge-reviewer` 可批准或拒绝；CAS 保证只消费一次。
- `GET /v1/wiki/pages`：读取页面版本及关系，动态投影过期和 superseded 状态。

Web Review 不把浏览器提交的正文当成 Evidence。`POST /api/review/runs/{run_id}/wiki-candidates`
先通过 Runtime Assignment 与数据域权限读取真实 Evidence 哈希，再调用本服务；候选决策由
`POST /api/review/wiki/candidates/{candidate_id}/decision` 转发。对应权限为 `knowledge:compile`、
`knowledge:review` 和 `knowledge:read`，批准者还必须拥有 `knowledge-reviewer` 角色。

所有业务接口要求 `X-Tenant-Id`、`X-User-Id` 和 `X-Knowledge-Wiki-Key`。生产还必须启用 OIDC；
Header 模式只用于本地 Compose。

## 下游闭环

审批事务同时产生四类事件：页面发布事实、RAG 摄取请求、评测请求和发布门禁请求。Relay 把批准后的
Markdown 正文送入 `/ingestion/wiki-pages`；摄取服务校验正文 SHA-256，以
`tenant + page_id + version` 生成稳定 Document/Job ID，再创建真实的 `PARSE` 作业。解析成功后才会写入
OpenSearch 投影，因此这里不是一条没有消费者的“伪 REINDEX”消息。

若新页显式 supersede 旧页，摄取 Worker 会在同一租户的索引内把旧页标记为 `superseded`；查询同时过滤
`knowledge_status != active` 与超过 `valid_until` 的页面。生命周期约束在索引内部生效，旧知识不会先参与
打分再被应用层丢弃。

Governance 同时建立 `PENDING_EVALUATION` 的 Knowledge Change Gate。该 Gate 绑定真实摄取 Job ID，明确
要求 Recall@K、Precision@K、MRR、nDCG 和 Faithfulness，不能自动标记为通过；Agent Lab/评测 Worker
完成 Golden Dataset 回归后，才可生成 Control Plane 能消费的正式质量门禁。

## 失败、重试与一致性

- 候选审批使用状态 CAS，同一审批不能消费两次。
- Wiki 页、关系和四条 Outbox 事件在一个数据库事务中提交，不在审批事务里同步调用下游。
- Relay 用租约支持多副本消费；失败后指数退避，超过 `relay_max_attempts` 进入 DLQ。
- RAG 摄取以 Wiki 页面版本为幂等键；同一版本内容摘要变化会返回冲突，而不是覆盖已确认知识。
- RAG 或 Governance 暂时不可用时，已批准 Wiki 页仍是权威记录；Outbox 可恢复投递，不会伪造门禁通过。
- 生产 PostgreSQL 是页面、候选和 Outbox 的事实来源；OpenSearch 只是可重建投影。

## 本地运行

```powershell
docker compose -f compose.platform.yaml up -d --build knowledge-wiki-service knowledge-wiki-relay
```

本地端口为 `9094`。测试：

```powershell
python -m pytest knowledge-wiki-service/tests -q
```

生产配置强制 PostgreSQL 与 OIDC；`database_url` 不满足 PostgreSQL 时服务启动失败，避免误把 SQLite
当成多副本生产存储。
