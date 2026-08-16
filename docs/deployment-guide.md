# Agent Platform 部署指南

## 本地联调

在仓库根目录复制各服务的 `.env.example` 后启动本地拓扑：

```powershell
docker compose -f compose.platform.yaml up --build -d
python scripts/platform_e2e.py
```

该配置用于开发和契约联调。它启动线上服务所需的进程、Model Lab、Agent Lab 和共享依赖。Context、
RAG Query、摄取 API 与摄取 Worker 虽共用 RAG 源码包，但在 Compose 中是独立工作负载；Runtime 使用
独立镜像，不安装 `rag-agent-service`。

## Agent Lab 发布门禁配置

只有在 Control Plane 启用以下配置时，Agent 正式发布才强制要求 Agent Lab 回放证据：

```dotenv
CONTROL_PLANE_AGENT_LAB_REQUIRED=true
CONTROL_PLANE_AGENT_LAB_BASE_URL=https://agent-lab:8092
CONTROL_PLANE_AGENT_LAB_SERVICE_API_KEY=<long-random-secret>
AGENT_LAB_SERVICE_API_KEY=<same-long-random-secret>
```

发布前先创建 laboratory Release，再由 Agent Lab 创建、准备和运行实验。Control Plane 会拒绝缺少
`agent_lab_experiment_id`、未通过 Gate、Agent/版本不一致、非 laboratory 环境，或 Gate 与实验 Judge Run
不一致的发布请求。

## 生产部署基线

`compose.production.yaml` 是变量和服务关系模板。实际生产应使用 Kubernetes、Nomad 或等价编排器，
并至少完成：

1. Runtime、LLM Gateway、Tool Gateway 多副本；Runtime Worker、RAG 摄取 Worker、Governance Consumer
   与在线 API 分开扩缩容。
2. PostgreSQL 取代本地 SQLite；Kafka + Debezium/Kafka Connect 读取 Transactional Outbox；配置幂等消费者、
   指数重试、DLQ 与受控重放。
3. OpenSearch/向量数据库实现带 ACL 的版本化索引；对象存储保留文档、实验工件和不可变审计导出。
4. OIDC/JWT、工作负载身份、每个工作负载独立 mTLS 证书、最小网络策略与 OPA 策略。
5. 独立 Temporal Cluster、区域化 Worker Queue、备份、恢复和跨区域故障转移演练。
6. Metrics、Trace、日志脱敏、SLO、告警、容量基线与恢复演练。

## Runtime Executor Catalog

每个可接收某环境流量的 Runtime Cluster 必须登记在 Control Plane 的版本化
`runtime-executors.json`：`cluster_id`、`environment`、`base_url` 和 `executor_profiles` 缺一不可。
生产 Control Plane 启用 `CONTROL_PLANE_RUNTIME_EXECUTOR_CATALOG_REQUIRED=true` 后，创建 Release 会同时
检查目录和 Runtime `/api/v1/agent/capabilities` 实例证明；任一候选实例不可达、版本不一致或缺少
`runtime_executor` Profile，发布即被拒绝。生产目录还应固定实例返回的 `capability_manifest_digest`：
它覆盖 Provider、契约版本、工件摘要、隔离级别和 Profile 兼容范围，可发现“目录版本未变但实际部署
Provider 已漂移”的情况。发布记录会保存 Catalog Version、SHA-256 摘要、Manifest 摘要和被验证的
Cluster ID，供审计和回滚对账。

## 发布 Artifact 与执行器选择

Control Plane 创建 Agent Version 时，会在同一事务内将 `PublishedSnapshot` 编译为不可变
`runtime-snapshot/v1` Artifact；Artifact 内容与版本一起计算 SHA-256。生产 Runtime 必须设置
`RUNTIME_SNAPSHOT_REQUIRED=true`，并只接受哈希一致的 Artifact，避免在请求到达时重新解释 Draft 或发生
配置漂移。

Runtime 启动期固定装配只读 Executor Catalog，当前 Profile 为：

1. `simple/v1`：无状态短执行；
2. `declarative-langgraph/v1`：默认的 LangGraph Agent 状态机；
3. `temporal-workflow/v1`：仅由异步 `POST /runs` 提交的长期可靠 Workflow。
4. `code-runner/v1`：仅用于已显式启用隔离 Sandbox Provider 的研发型 Agent；必须绑定版本固定的
   `controlled_code_runner` 工具，默认不会出现在 Runtime Catalog。

第三类 Profile 进入 `WAITING_APPROVAL` 时不会结束 Workflow。`POST /runs/{run_id}/resume` 只发送带审批人
和决定的 Temporal Signal；Worker 从已持久化的 LangGraph checkpoint 续跑。部署时应为 API 与 Worker 配置相同的
Artifact 校验密钥、区域化 Task Queue 路由和 Runtime Executor Catalog Version，否则发布前能力校验应拒绝流量。

## Runtime 配置命名与数据隔离

`agent-runtime` 只读取 `RUNTIME_*` 环境变量，已不再兼容 `RAG_*` 前缀。生产 Compose 通过独立
`x-runtime-env` 注入其数据库、Temporal、OIDC、mTLS、下游端点和服务密钥，Runtime Worker 同样继承
该环境。其运行记录和 Outbox 默认保存至 PostgreSQL `runtime_platform` Schema；Debezium 订阅也使用
`runtime_platform.runtime_outbox`。升级时必须同时更新运行服务、Worker 和 Kafka Connect 的表白名单，
不能仅修改 API 容器。

## Control Plane 与 Governance 容器级 mTLS

生产镜像中的 Control Plane 与 Governance 都由 `platform_infra.asgi_runner` 启动，而非直接运行
Uvicorn。Compose 为两个 API 工作负载分别注入服务器证书、私钥和平台 CA，并设置
`PLATFORM_MTLS_SERVER_ENABLED=true`；没有受 CA 信任的客户端证书，TCP 已建立后仍无法完成 TLS 握手。

这会影响全部直连调用方：Runtime、Agent Lab、RAG 系列服务、Tool Gateway 和 LLM Gateway 均改为
`https://agent-control-plane:8080` 或 `https://agent-governance:8081`。Python 服务复用各自工作负载的
`mtls_httpx_options`；LLM Gateway 则使用独立的 `platformServiceWebClientBuilder`，仅向平台内部服务发送
客户端证书，外部模型厂商请求仍使用普通 WebClient。容器健康检查也以同一证书访问 loopback ready
端点，避免探针成为无证书旁路。

## Agent Lab 的生产执行模型

生产 `compose.production.yaml` 将 Agent Lab 拆为 `agent-lab` API 和 `agent-lab-worker` 两个工作负载：

1. API 冻结快照并把唯一 `job_id` 写入 PostgreSQL，然后以确定的 `agent-lab/{job_id}` 提交 Temporal；
2. Worker 从独立 `agent-lab-experiments` Queue 消费，并在 PostgreSQL 中用行锁与租约领取任务；
3. 下游短暂故障按指数退避重试，超出 `AGENT_LAB_JOB_MAX_ATTEMPTS` 后写入持久化 DLQ，同时实验收口为
   `FAILED`；
4. Agent Lab API 入口与连接 Runtime、Control Plane、Governance 的链路均要求 OIDC 工作负载身份与 mTLS；
   Control Plane、Governance 的应用容器已直接以统一 ASGI mTLS 启动器监听，服务网格/Ingress 仍可作为额外
   的网络隔离层，但不再是这两条调用链获得双向认证的唯一条件。
   `release-evidence` 仍额外验证服务密钥，供凭据迁移期间纵深防御；
5. 实验聚合与调度任务分表保存，Temporal 不是事实存储，因此重放、审计和故障恢复不依赖工作流历史。

运行生产模板前必须提供 `AGENT_LAB_WORKLOAD_CLIENT_SECRET`、Agent Lab 独立客户端证书、OIDC 配置和
PostgreSQL/Temporal 可用性。部署层仍应实现 NetworkPolicy、实验输出分级脱敏、对象存储归档、保留期和
DLQ 告警/重放演练；代码不能替代这些基础设施控制。

## LLM Gateway 多维准入控制

LLM Gateway 把短期过载、长期成本和上游故障拆成三个独立边界：`admission` 管理 RPM、TPM、并发及请求大小；
`usage` 管理每日 Token/成本预占和结算；`resilience` 只管理熔断。生产 Compose 必须设置
`GATEWAY_ADMISSION_STORE=redis` 和 `GATEWAY_QUOTA_STORE=redis`；生产启动校验会拒绝 memory 模式。

`admission` 使用 Redis Lua 对 Tenant、User、Route 和 Provider 维度的 Token Bucket 与 in-flight 许可做原子判断。
请求完成、取消、流式连接断开或异常时释放并发许可；进程崩溃时依靠 `GATEWAY_CONCURRENCY_LEASE_TTL_SECONDS`
回收兜底许可。应按供应商合同额度设置 `GATEWAY_PROVIDER_RPM`、`GATEWAY_PROVIDER_TPM` 与
`GATEWAY_PROVIDER_MAX_CONCURRENCY`，再向下为租户和用户分配更小的公平使用上限。`GATEWAY_MAX_UPSTREAM_ATTEMPTS`
限制一个业务请求在 fallback 链中可触发的真实模型调用；生产启动校验同时要求 `gateway.max-retries=0`，从而避免
SDK、Gateway 和 fallback 多层重试叠加放大供应商流量。

所有本地准入拒绝返回 HTTP 429、受限维度和 `Retry-After`。不要让调用方立即重试；应遵循该响应头退避。
若所有模型候选均熔断，网关返回 503；若均受准入限制，返回 429，二者都不会被伪装成 502。

生产告警必须以 `reasonCode` 而非泛化的 HTTP 429 聚合：`ADMISSION_*` 是网关瞬时准入限制，
`QUOTA_DAILY_*` 是不可短时恢复的日配额，`PROVIDER_RATE_LIMIT` 是上游厂商限制。入口准入拒绝同样会异步导出
脱敏 Trace 与 `llm.request.rejected` 治理事件。Prometheus 除准入结果外还应采集
`llm_gateway_admission_utilization`、`llm_gateway_execution_total`、`llm_gateway_upstream_attempt_total` 和
`llm_gateway_provider_rate_limited_total`；后两个的比值揭示 fallback 造成的调用放大。所有指标标签都不得包含
tenant、user、route 名称或请求内容。
