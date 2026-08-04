# rag-agent-service

> The service has been split into independently deployable runtime, context,
> query and ingestion processes. See [docs/service-split.md](docs/service-split.md)
> for boundaries, startup commands and migration notes. The legacy
> `app.main:app` entrypoint remains available during migration.

面向 GMP 合规审查的独立 Python RAG + Agent 服务。它和 `llm-gateway` 分工清晰：本服务负责文档解析、法规/企业知识库、证据检索、条款审查和报告；所有聊天模型调用统一经过 Java `llm-gateway`。

```text
rag-agent-service -> llm-gateway -> DeepSeek / OpenAI / Qwen / Kimi / Claude / Ollama / vLLM
```

Python 服务只配置网关逻辑模型名。厂家密钥、路由、fallback、限额、成本和审计都由网关管理，不允许业务服务直连厂家聊天接口。

## 当前第一版能力

- FastAPI 服务骨架和健康检查
- 文件上传、本地文件存储、PDF/Word/Excel/Markdown/Text 基础解析
- 内置 GMP/ALCOA+ 示例法规和 checklist
- BM25 + 本地 Hash embedding 混合检索
- GMP checklist 覆盖率检查、风险评分、CAPA 建议
- Markdown 审查报告生成
- 为后续 MinIO、pgvector、PaddleOCR、Qwen/bge embedding、Ragas/Phoenix 预留清晰替换点

## 快速启动

建议使用 Python 3.11-3.13。当前部分上游依赖在 Python 3.14 上仍可能有兼容波动。

```powershell
cd C:\Users\Administrator\Documents\AI工作\rag-agent-service
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
uvicorn app.main:app --reload --port 8001
```

健康检查：

```text
GET http://localhost:8001/api/v1/health
GET http://localhost:8001/api/v1/health/ready
```

## LLM 网关配置

复制 `.env.example` 为 `.env`。本机同时运行两个服务时使用：

```env
RAG_LLM_ENABLED=true
RAG_LLM_GATEWAY_BASE_URL=http://localhost:8080
RAG_LLM_GATEWAY_API_KEY=
RAG_LLM_GATEWAY_USER_ID=rag-agent-service
RAG_LLM_MODEL=deepseek-v4-flash
RAG_LLM_STARTUP_CHECK=true
RAG_GENERATION_MODEL=deepseek-v4-flash
```

容器部署时把地址改为 `http://llm-gateway:8080`。`RAG_LLM_MODEL` 和 `RAG_GENERATION_MODEL` 必须是网关 `gateway.routes` 中注册的逻辑模型。厂家 API Key 只配置在 `llm-gateway`，不要放进本项目。

启动顺序：先启动 Java 网关并确认 `GET http://localhost:8080/actuator/health` 正常，再启动本服务。若启用 `RAG_LLM_STARTUP_CHECK`，网关不可达会阻止 RAG 服务启动；运行期间可由 `/api/v1/health/ready` 作为 readiness probe。

条款覆盖结果会返回 `COVERED/PARTIAL/MISSING/NOT_APPLICABLE/UNCERTAIN`。网关异常时不会伪装成正式结果，而会返回 `judge_method=KEYWORD_FALLBACK`、`degraded=true` 和具体原因。

## 常用接口

```text
POST /api/v1/documents/upload
POST /api/v1/documents/{document_id}/parse
GET  /api/v1/knowledge/regulations
POST /api/v1/knowledge/regulations/import
GET  /api/v1/knowledge/checklists
POST /api/v1/reviews/gmp
GET  /api/v1/reviews/{review_id}
POST /api/v1/reviews/{review_id}/rerun
POST /api/v1/agent/run
POST /api/v1/evaluations/run
GET  /api/v1/evaluations/{evaluation_id}
```

## GMP 审查示例

```powershell
Invoke-RestMethod `
  -Method Post `
  -Uri "http://localhost:8001/api/v1/reviews/gmp" `
  -ContentType "application/json" `
  -Body '{"content":"SOP编号 SOP-QA-001，版本 V1.0，生效日期 2026-01-01，批准人 QA经理。发生偏差后应记录、调查并完成CAPA。记录人张三，时间2026-07-09，保留原始记录并复核。"}'
```

报告会保存到 `data/reports/{review_id}.md`。

## 目录说明

```text
app/api              HTTP 接口
app/core             配置与日志
app/domain           领域模型和请求模型
app/ingestion        文件识别、解析、切块
app/knowledge        法规、checklist、文档仓库
app/retrieval        BM25、向量占位、混合检索
app/review           字段抽取、风险评分、CAPA、GMP 审查
app/report           Markdown 报告渲染
app/storage          本地存储，后续可替换 MinIO
app/infrastructure   llm-gateway 等外部适配器
```

## 后续替换路线

- `HashEmbeddingRetriever` 替换为 Qwen text-embedding-v3 或 bge-m3。
- `InMemoryRepository` 替换为 PostgreSQL + pgvector。
- `LocalFileStorage` 替换为 MinIO。
- `DocumentParser` 的图片和扫描 PDF 分支接入 PaddleOCR。
- `evaluation_api` 接入 Golden Dataset、Ragas 和 Phoenix trace。

## Agent Graph、Rerank 与 Tool Gateway

`POST /api/v1/agent/run` 使用持久化 LangGraph 执行完整在线规划与 Agent Loop：

```text
analyze(Intent + Entity + Source)
  -> assess(Complexity + SLA + Cost)
  -> build ExecutionPlan
  -> route
  -> retrieve / reason / tool / clarify
  -> answer
  -> safety
```

Runtime 在模型之外强制执行 Deadline、Cost、Step、LLM Call、Tool Call、Retrieval Round 和下游 Attempt Budget 七类硬限制。限制优先取 Control Plane 发布快照中的 `runtime_limits`，请求只能进一步收紧，不能放宽。模型、Prompt、Tool 及其版本同样绑定到已发布 Snapshot。

本地 Checkpoint 保存在 `data/runtime_checkpoints.db`，高风险工具返回 `PENDING_APPROVAL` 时 LangGraph 会持久化中断状态。审批系统在 Tool Gateway 完成审批后，调用 `POST /api/v1/agent/runs/{run_id}/resume` 恢复原 Run；Runtime 重启不会丢失等待中的执行。

每次响应包含 `execution_plan`、`budget` 和 `execution_trace`，可通过 `GET /api/v1/agent/runs/{run_id}` 查询。`X-Request-Id` 在租户内幂等；运行结果和 Governance Outbox 事件在同一个 SQLite 事务中提交。

```http
POST http://localhost:8001/api/v1/agent/run
X-Tenant-Id: demo-tenant
X-User-Id: demo
X-Permissions: rag:read,document:read
X-Request-Id: agent-debug-001
Content-Type: application/json

{
  "task": "检索审计追踪和记录保存要求，引用证据后回答",
  "session_id": "session-001",
  "max_steps": 6,
  "metadata": {"business": "gmp-review"}
}
```

生产请求应从 `llm-gateway /v1/agent/run` 进入，由 Gateway 鉴权后注入租户和内部权限。`session_id` 会与租户、用户组成 checkpoint namespace。Runtime 使用 `RAG_TOOL_GATEWAY_BASE_URL` 发现当前租户有权使用的工具，并把工具执行交给独立 `tool-gateway`。后者统一执行 JSON Schema 校验、权限检查、审批、幂等、限流、超时、重试、熔断和审计；模型不能调用目录外或无权使用的函数。

Runtime 只持有 Tool Gateway 的服务凭证，不持有审批管理员凭证或下游业务密钥。写工具的幂等键由 `request_id + tool_name + arguments` 稳定派生；高风险工具返回 `PENDING_APPROVAL`，后续由独立审批工作流处理。

每次运行会先从 Context Service 装载会话历史和用户上下文，再执行语义分析，因此历史消息会同时进入规划 Prompt 和每轮 Decision Prompt。需要知识检索时，Runtime 根据发布快照中的 `knowledge_bindings.required` / `failure_mode` 决定 RAG 是否为强依赖；仅当绑定明确配置为可选且 `failure_mode=memory_only` 时，RAG 故障才会降级到纯记忆上下文，强依赖仍会快速失败。

Context Service 使用角色、时间、查询相关性和来源可信度联合排序。`RAG_CONTEXT_MESSAGE_BUDGET_RATIO` 控制消息与证据的初始 Token 配额，未使用配额会按候选分数重新分配。返回的 `budget_report`、每项 `metadata.context_ranking`、`rag_status` 和 `degrade_reason` 可用于调试、观测和审计；入选历史消息最终仍按时间顺序交给模型，避免破坏对话语义。

生产环境设置 `RAG_REQUIRE_SERVICE_AUTH=true` 与 `RAG_SERVICE_API_KEY`，并保证它和 Gateway 的 `RAG_AGENT_API_KEY` 一致。RAG 会使用常量时间比较校验 `X-Rag-Agent-Key`，避免外部请求伪造租户或工具权限头。

Rerank 支持三种模式：

- `none`：保持 BM25 + 向量融合顺序。
- `cross_encoder`：本地 `sentence-transformers CrossEncoder`，安装 `pip install -e ".[rerank-local]"`。
- `vendor`：调用 Cohere-compatible `/rerank` HTTP API，密钥只从环境变量加载。

## OpenTelemetry

设置 `RAG_OTEL_ENABLED=true` 后，FastAPI 入站、HTTPX 出站、Agent、Retrieve、Rerank、Tool 和 Gateway 调用都会产生 Span，并使用 W3C `traceparent`/`baggage` 传播上下文。默认 OTLP/HTTP 地址为 `http://localhost:4318/v1/traces`。
