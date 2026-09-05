# 企业级 Agent Platform 生产环境配置说明书

> 适用版本：仓库 `main` 分支。最后更新：2026-09-05。  
> 目标：将当前已实现的服务、身份、审计、检索与恢复边界部署到真实生产基础设施；本文不是把本机 Compose 直接暴露到公网的操作手册。

## 1. 使用方式与责任边界

生产部署使用 `compose.production.yaml` 作为**变量与服务关系参考**。推荐由 Kubernetes、Nomad 或企业容器平台实现副本、Secret、工作负载身份、NetworkPolicy 与备份；如暂时使用 Compose，也必须使用独立生产主机、受管 Secret 文件和私有网络。

禁止把以下任何内容带入生产：`compose.platform.yaml`、`compose.retrieval.local.yaml`、本机 Keycloak、`local-*-key`、示例密码、开发 Header 身份模式、关闭安全插件的本机 OpenSearch Profile。生产 Compose 不应与本地 Compose 叠加。

| 角色 | 负责事项 | 不能替代 |
| --- | --- | --- |
| 平台运维 | 集群、网络、数据库、证书、备份、监控、容量与故障演练 | 业务审批和 Agent 发布决策 |
| 安全/身份团队 | IdP、角色/权限 Claim、MFA、工作负载身份、KMS 与密钥轮换 | 直接修改运行账本或证据 |
| 数据团队 | 数据源授权、对象存储保留、索引容量、RAG 评测 | 绕过 Evidence/ACL 直接写索引 |
| Agent 平台管理员 | 租户、用户、Agent Spec、Release、质量门禁与回滚 | 读取密钥或授予自身工作负载身份 |
| 业务负责人 | Golden Case、审批规则、坏例复核与上线验收 | 修改基础设施安全基线 |

## 2. 目标生产拓扑

```text
Internet / Corporate SSO
        │ HTTPS
        ▼
  Web BFF / Workspace ─── OIDC Authorization Code + PKCE
        │ mTLS + workload token
        ▼
Runtime ─ Context ─ RAG Query ─ Tool Gateway ─ LLM Gateway
   │          │          │              │              │
   ├── PostgreSQL / Redis ├── OpenSearch ├── Enterprise APIs
   ├── Temporal Workers   ├── Object Storage / KMS
   └── Outbox ─ Debezium / Kafka Connect ─ Governance / DLQ

Control Plane ── Release / Snapshot / Tool Catalog / Quality Gate
Governance ───── Audit / Evaluation / WORM export / online governance
```

在线 API 与异步 Worker 必须分开扩缩容：Runtime API、Runtime Worker、RAG Query、Ingestion API、Ingestion Worker、Governance Consumer/WORM Worker、Agent Lab Worker 都是独立工作负载。PostgreSQL、OpenSearch、Kafka、Redis、Temporal、对象存储与 IdP 需要使用企业受管服务或独立高可用集群。

## 3. 上线前的基础设施准备清单

### 3.1 必需依赖

| 依赖 | 生产要求 | 平台用途 |
| --- | --- | --- |
| PostgreSQL 17+ | 高可用、PITR、加密、专用数据库用户、`wal_level=logical` | Control Plane、Runtime、RAG、Governance、Outbox/CDC |
| Redis | TLS、ACL、持久化与高可用 | 配额、限流、服务端 Web Session、语义缓存 |
| Kafka + Kafka Connect | 至少三副本、ACL、TLS/SASL、监控 Consumer Lag | Debezium 投递 Outbox、Governance 消费、DLQ/重放 |
| Temporal | 独立 Cluster、持久化存储、命名空间备份 | 长流程、审批等待、Worker 恢复与跨区域调度 |
| OpenSearch | TLS、认证、分片/副本、快照仓库、版本化索引 | BM25、向量召回、索引内 ACL 与 Evidence 候选 |
| S3 兼容对象存储 | KMS、版本、保留策略、最小权限 Bucket Policy | 原始文档、Session Archive、模型工件、WORM 审计 |
| OIDC IdP | MFA、JWKS、Authorization Code + PKCE、Client Credentials | 人类登录与服务到服务身份 |
| OPA | 版本化策略、只读 Bundle/发布流程 | 运行权限与策略判定 |
| OTEL / Prometheus / Alertmanager | TLS、受控告警接收方、日志脱敏 | 指标、Trace、SLO 与告警 |

### 3.2 域名、网络与入口

1. 为浏览器仅公开一个 HTTPS 域名，例如 `https://agent.example.com`，入口只指向 `agent-web-bff`。
2. Runtime、Control Plane、Governance、RAG、Tool Gateway、LLM Gateway、数据库和 Worker **不直接暴露公网端口**。
3. Namespace/安全组采取默认拒绝：仅放行调用链所需的入站与出站连接。可从 [network-policies.yaml](../deploy/kubernetes/network-policies.yaml) 开始适配。
4. 外部模型、企业 API、对象存储和 IdP 的出站规则必须列出 FQDN/私网端点；禁止 Runtime/Tool Worker 任意访问互联网。
5. 为 BFF 配置 WAF、TLS 终止、HSTS、受控 CORS 与访问日志脱敏；不要在 CDN、反向代理或浏览器记录 `Authorization`、Cookie、模型 Key。

## 4. 配置来源与 Secret 管理

`.env.production.example` 是**变量目录**，不是可直接复制的密钥文件。所有 `secret-manager-reference`、`compatibility-secret`、`*.example.com` 与 `/secure/...` 都必须替换。

推荐的注入顺序：

1. Secret Manager/Vault 保存机密；
2. CI/CD 仅获得读取部署所需 Secret 的短期工作负载身份；
3. 编排器将 Secret 挂载为只读文件或受限环境变量；
4. 服务启动期校验必填项，缺失即失败；
5. 轮换 Secret 后滚动重启对应工作负载，并撤销旧值。

不要把 `.env.production`、私钥、证书、数据库 DSN、厂商 API Key、WORM 签名 Key、浏览器 Cookie 或导出的审计正文提交到 Git、镜像层、前端构建产物或工单附件。

### 4.1 最小 Secret 分类

| 分类 | 示例变量/文件 | 规则 |
| --- | --- | --- |
| 数据库/缓存 | `POSTGRES_PASSWORD`、`REDIS_PASSWORD` | 不同服务使用不同数据库角色或 ACL；禁止超级用户运行应用。 |
| OIDC 客户端 | `*_WORKLOAD_CLIENT_SECRET`、`WEB_BFF_OIDC_CLIENT_SECRET` | 每个工作负载独立 client；不可复用 Runtime、RAG、Knowledge Wiki Relay 或 BFF 凭据。 |
| 服务兼容密钥 | `*_SERVICE_API_KEY`、`*_INTERNAL_SERVICE_API_KEY` | 仅作为 OIDC/mTLS 迁移期的纵深校验；应缩小权限并规划移除。 |
| 模型/工具 | `RUNTIME_LLM_GATEWAY_API_KEY`、供应商 Key | 浏览器与 Desktop 永不持有；Gateway 负责路由。 |
| 存储/KMS | S3 IAM 工作负载身份、`S3_KMS_KEY_ID` | 不使用长期 Access Key；每个 Bucket 单独最小权限。 |
| TLS | `AGENT_MTLS_CA_FILE`、各 `*_MTLS_CERT_FILE` / `*_KEY_FILE` | 每个工作负载一对叶证书；私钥只读挂载，禁止共享。 |

## 5. IdP、租户、用户与权限配置

### 5.1 人类账号与机器身份必须分离

- `user_id` 是 IdP Token 的稳定主体标识（通常为 `sub`），用于 Run 所有者、审查人和审计归属；不得用登录名替代。
- `tenant_id` 是平台数据隔离目录中的租户标识，出现在用户属性、Token Claim、RAG 文档 ACL、Run、Release 与审计事件中；它不是用户 ID。
- 人类用户通过 Authorization Code + PKCE 登录；服务通过 Client Credentials/工作负载身份获取 Token。
- Desktop 只是 Workspace 的本机能力连接器，使用短期配对关系；不应拥有更高管理员权限。

### 5.2 IdP 必须提供的 Access Token Claim

| Claim | 生产约束 | 用途 |
| --- | --- | --- |
| `iss` | 必须等于 `OIDC_ISSUER` | 发行方校验 |
| `aud` | 必须包含 `OIDC_AUDIENCE` | 受众校验 |
| `sub` | 不可变、不可复用、非邮箱 | 映射平台 `user_id` |
| `exp` / `iat` | 短期有效且可验证 | 防重放与会话续期 |
| `tenant_id` | 单个当前租户；切换租户重新签发 Token | 数据、Run、审计与目录隔离 |
| `roles` | 受控角色集合 | Workspace / Review / Console 视图与职责 |
| `permissions` | 精细动作集合，例如 `rag:read`、`agent:review`、`run:review:assign` | 服务端授权，不信任浏览器提交的 Header |

平台建议至少定义 `agent-user`、`agent-reviewer`、`tenant-admin`、`platform-admin` 与各工作负载 Service Account。最高管理员不能通过 Web UI 把 `platform-admin` 或机器身份任意转授给普通账号；该操作应只在 IdP 安全管理员流程中完成。

### 5.3 BFF 与 IdP 注册

为 `agent-web-bff` 注册 confidential client：

- Redirect URI：`https://<公网域名>/auth/callback`；
- Post logout redirect URI：`https://<公网域名>/`；
- Flow：Authorization Code + PKCE；禁止 Implicit Flow；
- 使用 HTTPS、`HttpOnly`、`Secure`、`SameSite=Lax/Strict` 会话 Cookie；
- 配置 `OIDC_AUTHORIZATION_URL`、`OIDC_TOKEN_URL`、`WEB_BFF_OIDC_END_SESSION_URL`、`WEB_BFF_OIDC_CLIENT_ID`、`WEB_BFF_OIDC_CLIENT_SECRET`、`WEB_BFF_PUBLIC_ORIGIN`、`WEB_BFF_OIDC_REDIRECT_URI`；
- 管理员敏感操作必须要求 IdP 近期 MFA/ACR，且记录审计原因。

## 6. mTLS 与服务身份

每个工作负载使用独立的叶证书和独立 OIDC Client：Runtime API、Runtime Connector Relay、Artifact Ingestion Relay、Secondary Worker、Context、RAG Query、Ingestion API、Ingestion Worker、Control Plane、Governance、Governance WORM Worker、Tool Gateway、LLM Gateway、Web BFF、Agent Lab、Model Lab、Prometheus/Blackbox Exporter、Knowledge Wiki API/Relay。生产变量中 `KNOWLEDGE_WIKI_SERVICE_API_KEY` 与 `KNOWLEDGE_WIKI_RELAY_WORKLOAD_CLIENT_SECRET` 也必须由 Secret Manager 提供。

证书要求：

1. SAN 包含服务 DNS 名与必要的集群服务名；不以 Common Name 作为唯一身份；
2. CA、叶证书与私钥从 Secret Manager 或 cert-manager 注入，私钥不可写、不可导出到日志；
3. 服务端验证客户端证书，客户端验证 CA 与服务 DNS；
4. 证书有效期尽量短，至少提前 30 天告警、自动轮换并验证滚动重启；
5. 仅 TLS 不等于授权：每次请求仍验证工作负载 Token、目标 Audience 与最小权限。

`compose.production.yaml` 中所有 `*_MTLS_CERT_FILE`、`*_MTLS_KEY_FILE` 与 `AGENT_MTLS_CA_FILE` 都必须有实际挂载。不要让多个工作负载复用同一 `rag_runtime_cert` 或通配私钥。

## 7. 数据、检索与模型配置

### 7.1 PostgreSQL 与 CDC

- 使用独立 Schema：例如 `control_plane`、`runtime_platform`、`rag_platform`、`governance`；
- 启用 TLS、加密备份、PITR 与每季度恢复演练；
- 为 Debezium 配置逻辑复制、复制槽、publication 和专用只读复制用户；
- 部署后运行 `py -3.12 scripts/production_readiness.py --connect-url <Kafka Connect URL>`，要求 connector 与全部 task 为 `RUNNING`；
- 监控 replication slot 延迟、Kafka Connect 失败、Governance consumer lag、DLQ 深度和重放次数；
- 状态写入与 Outbox 必须在同一业务事务内；禁止在事务中同步发 Kafka 作为唯一审计路径。

### 7.2 OpenSearch 与 RAG

- `RAG_SEARCH_BACKEND=opensearch`，启用认证、TLS、快照仓库、索引副本与 shard 容量规划；
- 每个索引版本绑定一个 `EmbeddingContract`；更换 BGE-M3、Qwen3-Embedding 或云模型时必须新建索引版本，不得混写；
- 生产 `RAG_EMBEDDING_PROVIDER` 只能使用已评测的 `cloud_dashscope`、`qwen` 或 `local_openai`，且 `RAG_EMBEDDING_ALLOW_HASH_FALLBACK=false`；
- 在 Control Plane 将 `IndexBuildManifest=READY`、检索 Golden Dataset 和 Embedding Contract 绑定到 Release 后，才能激活 Alias；
- 索引仅是派生投影。权威文档仍放 PostgreSQL/对象存储，索引损坏应由 `REINDEX_KNOWLEDGE_BASE` 重建，不能把索引当备份；
- Redis 语义缓存只可使用 `redis` 或 `disabled`；其键必须包括 Tenant、用户/授权摘要、索引 Manifest、Embedding Contract 与检索档位。

### 7.3 模型与 Gateway

- 上游厂商 Key 或自部署模型凭据只配置在 LLM Gateway/受管推理服务，不进入 Runtime、Web、Desktop 或 Release Snapshot；
- 每条逻辑路由固定 `routeVersion` 与 `modelRevision`，防止供应商“同名模型”漂移；
- Gateway 配置 RPM、TPM、并发、成本、熔断与 fallback，并为每个租户设置 Token/USD 配额；
- 变更模型、Prompt、Judge、Rubric、温度或输出 Schema 时，先完成专家校准集、Golden/RAG 指标、分组 Hard Gate，再进行 Shadow → Canary → Promote；
- 供应商模型、Embedding、自部署 vLLM/Ollama 的网络出口和数据保留条款应由安全/法务批准。

## 8. 对象存储、WORM 与备份

建立至少四个逻辑 Bucket，并使用独立 IAM Policy：

| Bucket | 变量 | 最低要求 |
| --- | --- | --- |
| 原始文档 | `DOCUMENT_BUCKET` | KMS、版本、来源/租户前缀隔离、恶意文件扫描。 |
| Session Archive | `SESSION_ARCHIVE_BUCKET` | KMS、版本、留存期、只允许 Runtime Archive 写入。 |
| 模型工件 | `MODEL_ARTIFACT_BUCKET` | 不可变版本、模型卡、哈希、审批后读取。 |
| 审计/WORM | `AUDIT_BUCKET` | Object Lock/WORM、保留策略、独立签名/审计角色。 |

备份策略至少覆盖 PostgreSQL PITR、OpenSearch Snapshot、Temporal 持久化库、Kafka Topic/Connect 配置、OPA Bundle、Control Plane Snapshot Artifact、对象存储版本与 IdP 配置导出。每项必须设定 RPO/RTO、责任人、恢复 Runbook 和演练频率；“有备份”不等于“可恢复”。

## 9. Temporal 与跨区域故障转移

单区域先完成 Worker Queue 隔离、超时、重试、DLQ、幂等工具调用和恢复测试。跨区域时：

1. 建立 Temporal Global Namespace 与双区域复制；
2. 设置 `RUNTIME_TEMPORAL_GLOBAL_NAMESPACE_ENABLED=true`，填入主/次区域 Target 与 `*_TEMPORAL_WORKER_REGION`；
3. 次区域 Worker 使用独立证书、独立 OIDC Client 和本地 Temporal Frontend；
4. 在变更窗口运行 `scripts/invoke-temporal-failover-drill.ps1` 的 Dry Run；经批准后才使用 `-Execute`；
5. 验证 backlog、Workflow 唯一性、审批恢复、工具幂等键、成本账本、审计事件和回切后的状态对账。

不要把 DNS 切换或简单重启 Worker 宣称为故障转移演练。

## 10. 容量、SLO 与告警

先以压测与业务基线确定阈值，不能直接照搬本机数值。每个生产环境至少定义：

- Runtime：排队时间、成功率、P95/P99、恢复成功率、预算拒绝率；
- LLM Gateway：RPM/TPM、并发、供应商错误率、fallback 比例、成本与限流 `reasonCode`；
- RAG：Recall@K、关键证据漏召回率、P95、索引滞后、缓存命中率、ACL 拒绝；
- Tool Gateway：批准/拒绝、幂等复用、上游错误、执行超时、风险操作数；
- CDC/Governance：Outbox 延迟、Kafka Connect 状态、consumer lag、DLQ 深度、WORM 导出失败；
- 基础设施：PostgreSQL 连接/复制/PITR、Redis 内存、OpenSearch heap/shard/snapshot、Temporal backlog、对象存储错误率、证书有效期。

告警必须包含 Runbook 链接、租户/正文脱敏规则、责任组与升级路径。禁止把任务正文、模型 Prompt、证据正文、Token 或密钥写入指标标签和告警标题。

## 11. 分阶段上线与验收

### 阶段 A：基础设施验收

1. 在 CI 执行 `py -3.12 scripts/production_readiness.py`；
2. 渲染生产模板：`docker compose --env-file <受管环境文件> -f compose.production.yaml config --quiet`；
3. 验证 Secret 未出现在渲染日志、镜像层和仓库；
4. 检查 NetworkPolicy、mTLS 双向握手、OIDC JWT、Client Credentials、OPA 拒绝路径；
5. 验证数据库、对象存储、OpenSearch 和 Kafka Connect 的备份/恢复最小闭环。

### 阶段 B：功能与治理验收

1. 发布一个最小 Agent Draft → Version → Snapshot → Shadow；
2. 用 Golden Dataset 验证 RAG Recall、引用正确性、Faithfulness、Prompt Injection 红队与高风险零失败规则；
3. 验证工具权限、审批一次性消费、取消、Steering、重试和幂等；
4. 验证 Tenant A 无法读取 Tenant B 的 Run、证据、Artifact、审计或缓存；
5. 验证 WORM 审计导出、模型路由 Canary、回滚与异常告警。

### 阶段 C：逐步放量

按 Shadow → 内部 Canary → 小流量生产 → 全量生产进行。每一阶段都需要固定 Release/Snapshot、模型路由版本、索引 Manifest、质量门禁 Run、观测窗口、回滚触发条件和责任人。发生高风险 RAG 漏召回、工具越权、审计中断、预算异常或身份校验失败时，暂停 Promote 并按 Runbook 回滚。

## 12. 上线签核清单

- [ ] 每个服务副本、Worker、数据库、队列、索引和存储均有明确 owner；
- [ ] 所有示例 Secret、开发 Header、默认管理员与本机 Keycloak 已移除；
- [ ] 人类与工作负载身份分离，Token Claim 与 Tenant/权限映射已通过集成测试；
- [ ] mTLS 证书独立、SAN 正确、轮换与到期告警已演练；
- [ ] PostgreSQL PITR、OpenSearch Snapshot、对象存储版本/WORM、Temporal 恢复均已验证；
- [ ] Debezium Connector/Task、Governance Consumer、DLQ 与受控重放均正常；
- [ ] RAG 索引、Embedding Contract、Golden 检索指标和 Release Snapshot 一致；
- [ ] 模型版本、Prompt、Judge、温度、输出 Schema 均冻结并通过质量门禁；
- [ ] 监控面板、SLO、告警接收方和故障 Runbook 已被实际触发验证；
- [ ] Shadow/Canary/Promote/Rollback 与 Temporal 跨区域演练留有审计记录。

完成以上签核，才可以将该环境定义为“生产就绪”。
