# LLM Gateway

基于 Java 21、Spring Boot 3、Spring WebFlux 自研的大模型统一访问层。项目对标 LiteLLM 的核心思路，对外提供 OpenAI-compatible 接口，对内统一接入云端模型和本地推理服务，并在网关层沉淀协议适配、模型路由、fallback、SSE、限额、成本、Prompt/响应安全、审计和模型指标。

> **职责边界**：Gateway 不负责编排 Agent Graph、保存会话记忆、执行 RAG 检索、编排业务 Tool 或判断 Agent 下一步动作。这些能力属于独立的 `rag-agent-service`。完整边界、身份 Header 和成本预算协议见 [网关职责边界与调用协议](docs/网关职责边界与调用协议.md)。

## 核心能力

- 统一聊天接口：`POST /v1/chat/completions`，兼容 OpenAI Chat Completions 风格。
- SSE 流式输出：支持 `Accept: text/event-stream` 和 `stream=true`。
- 多模型接入：DeepSeek、OpenAI、通义千问、Kimi、Claude、Ollama、vLLM。
- 协议适配：OpenAI-compatible client + Anthropic Messages client。
- 模型路由：primary、fallback、灰度路由、权重负载均衡。
- 稳定性治理：超时、重试、限流、熔断、故障 fallback。
- 多租户 API Key：支持租户、用户、模型白名单。
- 限额控制：memory 单机版和 Redis Lua 原子扣减版。
- MySQL 持久化：请求缓存、成本报表、性能报表、评估资产、Phoenix Trace、合规审查、审计日志、模型热更新配置。
- Admin 鉴权：`/admin/**` 默认启用 Basic Auth，避免管理接口裸奔。
- 请求缓存：按租户隔离，支持 TTL；启用 MySQL 持久化后缓存内容落库，流式请求自动跳过缓存。
- Prompt 模板：支持 `prompt_template`、`template_id` 和 `{{变量}}` 渲染。
- 成本报表：按 provider、model、user、tenant 聚合 token 和费用。
- 性能报表：记录 TTFT、TPOT、QPS、Tokens/s、错误率、超时率、成本/request。
- LangChain4j 轻量 RAG 与 Tool Calling 仅作为历史兼容演示，默认关闭，不属于生产 Gateway 主链。
- RAG/Agent 评估治理：Prompt 版本、检索策略、Golden Dataset、回归测试、Ragas 风格指标、Phoenix 风格 trace。
- 合规审查闭环：Few-shot 风险判断、JSON Schema 校验、缺陷标注、CAPA 建议、人工确认、审计日志。
- 面经治理能力：Bad Case 记录、Prompt 实验、输出契约、后处理链、Redis 数据结构示例、缓存防穿透/击穿/雪崩说明。
- Admin REST 控制台：模型配置热更新、报表查询、缓存清理、健康状态查看。

## 技术栈

- Java 21
- Spring Boot 3.3.5
- Spring WebFlux / Reactor
- Reactor Netty WebClient
- Spring Boot Actuator
- Spring Data Redis
- Spring JDBC
- Spring Security
- MySQL Connector/J
- Micrometer Prometheus
- LangChain4j
- Maven
- JUnit 5

## 项目结构

```text
src/main/java/com/zxf/ai/gateway
|-- admin          # 模型、provider、route 热更新
|-- auth           # API Key、租户、用户、模型权限
|-- cache          # 请求缓存
|-- client         # OpenAI-compatible 和 Anthropic client
|-- compliance     # 合规风险审查、CAPA、人工确认、审计日志
|-- config         # 网关配置绑定、WebClient/代理配置
|-- enhancement    # LangChain4j RAG 与 Tool Calling
|-- eval           # Prompt 版本、检索策略、Golden Dataset、回归评估
|-- model          # 网关通用模型对象
|-- prompt         # Prompt 模板渲染
|-- report         # 成本报表、模型性能报表
|-- resilience     # 限流、熔断、健康状态
|-- routing        # 模型路由、fallback、灰度、权重
|-- service        # 主聊天网关链路
|-- usage          # token 估算、成本估算、memory/Redis 限额
`-- web            # REST Controller 和全局异常处理
```

## 快速启动

进入工程目录：

```powershell
cd C:\Users\Administrator\Documents\AI工作\llm-gateway
```

配置模型密钥，至少配置一个可用 key：

```powershell
$env:DEEPSEEK_API_KEY="你的 DeepSeek Key"
$env:OPENAI_API_KEY="你的 OpenAI Key"
$env:DASHSCOPE_API_KEY="你的通义千问 Key"
$env:MOONSHOT_API_KEY="你的 Kimi Key"
$env:ANTHROPIC_API_KEY="你的 Claude Key"
```

本地推理服务可选：

```powershell
$env:OLLAMA_BASE_URL="http://localhost:11434/v1"
$env:VLLM_BASE_URL="http://localhost:8000/v1"
$env:VLLM_MODEL="Qwen2.5-7B-Instruct"
```

运行测试：

```powershell
mvn test
```

打包：

```powershell
mvn clean package -DskipTests
```

启动：

```powershell
java -jar target\llm-gateway-0.1.0.jar
```

健康检查：

```text
http://localhost:8080/actuator/health
```

## MySQL 持久化与 Admin 鉴权

当前工程默认启用 MySQL 持久化和 Admin Basic Auth。

MySQL 默认连接 `localhost:3306/llm_gateway`，用户名 `root`，密码使用本地开发默认值 `970402`。启动时会执行 `src/main/resources/schema.sql` 自动创建运行状态表。

可通过环境变量覆盖：

```powershell
$env:MYSQL_URL="jdbc:mysql://localhost:3306/llm_gateway?useUnicode=true&characterEncoding=utf8&serverTimezone=Asia/Shanghai&createDatabaseIfNotExist=true"
$env:MYSQL_USERNAME="root"
$env:MYSQL_PASSWORD="970402"
```

如果临时不想连接 MySQL，可以关闭持久化：

```powershell
$env:GATEWAY_PERSISTENCE_ENABLED="false"
$env:MYSQL_INIT_MODE="never"
```

Admin 默认账号是 `admin/admin123`，可以通过 `ADMIN_USERNAME` 和 `ADMIN_PASSWORD` 修改。

访问 Admin 接口需要 Basic Auth：

```powershell
$pair = [Convert]::ToBase64String([Text.Encoding]::ASCII.GetBytes("admin:admin123"))
Invoke-RestMethod -Uri "http://localhost:8080/admin/overview" -Headers @{ Authorization = "Basic $pair" }
```

已落库的状态包括：请求缓存、成本报表、性能报表、模型配置热更新记录、RAG/Agent 评估资产、Golden Dataset、Phoenix Trace、合规审查结果、人工确认记录、审计日志、Bad Case、Prompt 实验、输出契约、后处理链和 Agent Memory。

## 面经问题对应的工程落点

| 面经问题 | 工程体现 |
|---|---|
| 模型不听指令、bad case 定位 | `/admin/engineering/bad-cases` 保存 prompt、模型参数、模型输出、期望输出、定位方法、根因、修复策略、评测证据 |
| Prompt、模型参数、后处理、流程约束 | `/admin/engineering/output-contracts`、`/admin/engineering/post-process-chains`、`/admin/engineering/prompt-experiments` |
| 解决后有没有评测数据 | Golden Dataset、Regression Run、Prompt Experiment 的 `metricSummary` |
| 反思结果污染上下文 | `AgentMemoryService` 将 reflection notes 与 session context 分层隔离 |
| Claude Code Memory 分层 | project rules、user preferences、session context、reflection notes 四层记忆 |
| Redis 数据结构 | `/admin/engineering/redis/demo` 写入 String、Hash、ZSet、Stream 示例 |
| Redis 过期和淘汰 | quota/cache/session key 使用 TTL；请求缓存支持随机 TTL 抖动 |
| 缓存穿透、击穿、雪崩 | `RequestCacheService` 提供合法请求边界、互斥保护、随机 TTL jitter |
| Redis Stream vs Kafka | Redis Stream 用作轻量审计/评估事件示例，高吞吐持久消息建议 Kafka |
| AI 编程工具流程 | README、HTTP 用例、测试、代码注释和 bad case 记录共同沉淀开发规则与复盘证据 |

## 主聊天接口

非流式调用：

```http
POST http://localhost:8080/v1/chat/completions
Content-Type: application/json
X-User-Id: demo

{
  "model": "deepseek-v4-flash",
  "messages": [
    {"role": "system", "content": "You are a concise AI engineering assistant."},
    {"role": "user", "content": "解释 LLM Gateway 的价值"}
  ],
  "temperature": 0.2
}
```

流式调用：

```http
POST http://localhost:8080/v1/chat/completions
Content-Type: application/json
Accept: text/event-stream
X-User-Id: demo

{
  "model": "deepseek-v4-flash",
  "stream": true,
  "messages": [
    {"role": "user", "content": "用三点说明 WebFlux SSE 的实现要点"}
  ]
}
```

当前支持的逻辑模型路由：

```text
deepseek-v4-flash
deepseek-v4-pro
gpt-4o-mini
qwen-plus
qwen-turbo
qwen-max
kimi-chat
kimi-long
claude-sonnet-4
claude-opus-4
claude-3-5-haiku
ollama-llama3.1
ollama-qwen2.5
vllm-local
```

## Prompt 模板

请求中可以传：

```json
{
  "model": "deepseek-v4-flash",
  "prompt_template": "interview-answer",
  "variables": {
    "topic": "模型路由、fallback 和熔断限流",
    "project": "Java WebFlux LLM Gateway"
  }
}
```

处理流程：

```text
prompt_template/template_id
        -> PromptTemplateService
        -> application.yml 中的 gateway.prompt-templates
        -> 替换 {{变量}}
        -> 自动注入 system/user messages
        -> 进入 ChatGatewayService 主链路
```

## 多租户 API Key

请求可以通过 `X-Api-Key` 或 `Authorization: Bearer` 传入租户 key。

示例：

```http
POST http://localhost:8080/v1/chat/completions
Content-Type: application/json
X-Api-Key: dev-demo-key

{
  "model": "deepseek-v4-flash",
  "messages": [
    {"role": "user", "content": "说明多租户 API Key 管理的作用"}
  ]
}
```

API Key 支持：

- `tenantId`
- `userId`
- enabled/disabled
- allowedModels 模型白名单

## 限额与 Redis 原子扣减

本地演示默认使用 memory：

```yaml
gateway:
  quota-store: memory
```

多实例生产环境建议使用 Redis：

```powershell
$env:GATEWAY_QUOTA_STORE="redis"
$env:REDIS_HOST="localhost"
$env:REDIS_PORT="6379"
```

Redis 实现使用 Lua 脚本把“检查每日 token/成本限额 + 预扣 prompt token”放在一个原子操作里，避免多实例并发时多个请求同时检查通过导致超额。

## 路由、fallback、灰度和权重

配置示例：

```yaml
routes:
  deepseek-v4-flash:
    primary: deepseek:deepseek-v4-flash
    weighted:
      - target: deepseek:deepseek-v4-flash
        weight: 90
      - target: qwen:qwen-plus
        weight: 10
    canary:
      - target: kimi:moonshot-v1-8k
        percent: 0
    fallbacks:
      - deepseek:deepseek-v4-pro
```

含义：

- primary：默认模型。
- fallback：失败后按顺序降级。
- weighted：按权重分流。
- canary：小比例灰度新模型。

## 成本报表

每次成功调用会记录：

- requestId
- tenantId
- userId
- provider
- model
- upstreamModel
- promptTokens
- completionTokens
- totalTokens
- cost
- latencyMs

接口：

```text
GET    /admin/reports/cost
GET    /admin/reports/cost/daily
DELETE /admin/reports/cost
```

## 模型性能报表

用于模型选型和容量评估，记录：

- TTFT：首 token 延迟。
- TPOT：平均每个输出 token 生成耗时。
- QPS：观测窗口内吞吐估算。
- Tokens/s：token 吞吐。
- errorRate：错误率。
- timeoutRate：超时率。
- costPerRequest：单请求平均成本。

接口：

```text
GET    /admin/reports/performance
GET    /admin/reports/performance/daily
DELETE /admin/reports/performance
```

## LangChain4j RAG 与 Tool Calling

增强接口：

```text
POST /v1/enhanced/chat
```

示例：

```http
POST http://localhost:8080/v1/enhanced/chat
Content-Type: application/json

{
  "message": "结合知识库说明这个 LLM Gateway 的架构，并调用工具列出当前能力"
}
```

当前增强层包含：

- `SimpleKnowledgeBase`：轻量知识库检索。
- `LangChain4jConfig`：ChatModel、ContentRetriever、Assistant 配置。
- `GatewayTools`：工具调用示例，包括成本估算、网关能力查询。
- `EnhancedChatService`：RAG + Tool Calling 编排入口。

说明：LangChain4j 是增强层，不替代 WebFlux 主网关链路。主链路负责网关基础设施能力，LangChain4j 负责 RAG、Tool Calling 和 Agent 风格编排。

## RAG/Agent 评估治理

评测资产、Judge、质量门禁、线上样本和 Golden 候选现在由独立
`agent-governance` 持有。以下 Gateway 接口作为兼容代理保留，旧前端和 CI
脚本无需立即修改；Gateway 本身不再保存评测状态或执行评测工作流。

接口：

```text
GET  /admin/eval
PUT  /admin/eval/prompt-versions
PUT  /admin/eval/retrieval-strategies
PUT  /admin/eval/golden-dataset
POST /admin/eval/regression-runs
PUT  /admin/eval/judge-rubrics
POST /admin/eval/judge-runs
POST /admin/eval/judge-runs/{runId}/quality-gate
POST /v1/feedback
GET  /admin/eval/governance
POST /admin/eval/governance/samples/{sampleId}/judge
POST /admin/eval/governance/samples/{sampleId}/review
POST /admin/eval/governance/golden-candidates/{candidateId}/review
POST /admin/observability/phoenix/traces
```

已支持：

- PromptVersion：Prompt 名称、版本、模型、参数、模板内容。
- RetrievalStrategy：embedding 模型、向量库、topK、scoreThreshold、rerank。
- Golden Dataset：标准问题、标准答案、参考上下文、标签。
- RegressionRun：按 Golden Dataset 跑轻量回归。
- Ragas 风格指标：answerSimilarity、contextRecall、faithfulness。
- Phoenix 风格 trace：traceId、requestId、spanName、input、output、metadata。
- LLM Judge：自动调用被测模型，使用两个独立 Judge Model 按版本化 Rubric 输出结构化评分。
- 多 Judge 仲裁：总分差距超过阈值或 PASS/BLOCK 结论不一致时，调用第三个模型仲裁。
- CI 质量门禁：按平均分、通过率、失败用例数和仲裁率做确定性判断，失败返回 `exitCode=1`。
- 线上闭环：自动采集请求/响应 Trace，完成 Schema、格式、usage 计算、超时、异常和成本检测。
- 用户反馈：Playground 点赞/点踩绑定 `X-Request-Id`；点踩样本落库后异步强制执行 Judge。
- 失败分流：高置信度自动归档 Bad Case，中置信度进入人工审核，低置信度进入普通抽检池。
- Golden 治理：自动生成候选，但普通候选也必须人工确认；法律、财务、安全等高风险候选必须专家审批。

LLM Judge 默认由 Governance 使用 `qwen-plus`、`claude-3-5-haiku` 和
`deepseek-v4-pro` 三个逻辑模型，并统一通过 Gateway 调用。模型和门禁阈值通过
`GOVERNANCE_JUDGE_*`、`GOVERNANCE_QUALITY_GATE_*` 配置。
本地执行门禁：

```powershell
$env:ADMIN_USERNAME="admin"
$env:ADMIN_PASSWORD="你的管理密码"
.\scripts\llm-judge-gate.ps1
```

完整设计、JSON 结构和 CI 配置见 [LLM Judge 自动评估](docs/LLM-Judge自动评估.md)。
线上反馈到 Golden Dataset 的完整状态机见 [线上评估与 Golden 闭环](docs/线上评估与Golden闭环.md)。

## 合规审查闭环

合规工作流由 `agent-governance` 实现并持久化。Gateway 只完成入口鉴权并代理旧
接口；AI 初审需要模型时，Governance 再以受信服务身份调用 Gateway。

接口：

```text
POST /v1/compliance/reviews
GET  /admin/compliance
GET  /admin/compliance/reviews
GET  /admin/compliance/reviews/{reviewId}
POST /admin/compliance/reviews/{reviewId}/confirm
GET  /admin/compliance/audit-logs
```

能力对应：

| 环节 | 实现 |
|---|---|
| AI 提示 | Governance 的 `ComplianceService` 组织审查提示，Gateway 只执行模型调用 |
| 缺陷标注 | 后端校验 JSON 字段，固定 `type/severity/evidence/reason/clause/confidence` |
| CAPA 建议 | Governance 校验纠正措施、预防措施、负责人、期限和验证方式 |
| 人工确认 | 高风险、模型要求复核、Schema 校验失败进入 `PENDING_HUMAN_REVIEW` |
| 审计日志 | Governance 记录 AI 输出、人工修改和最终确认 |

AI 审查示例：

```http
POST http://localhost:8080/v1/compliance/reviews
Content-Type: application/json
X-Api-Key: dev-demo-key

{
  "businessId": "BATCH-2026-0706-001",
  "documentType": "batch-release-record",
  "model": "deepseek-v4-flash",
  "reviewerHint": "Focus on deviation handling and product release controls.",
  "content": "Temperature excursion occurred during storage. No deviation record was opened. Product was released by QA on the same day.",
  "metadata": {
    "site": "demo-site",
    "system": "QMS"
  }
}
```

人工确认示例：

```http
POST http://localhost:8080/admin/compliance/reviews/{reviewId}/confirm
Content-Type: application/json

{
  "reviewer": "qa-manager",
  "finalRiskLevel": "HIGH",
  "finalSummary": "Confirmed high risk: product release happened without deviation investigation.",
  "decision": "CONFIRMED",
  "notes": "Human reviewer confirmed the AI finding and refined CAPA wording."
}
```

## Admin 控制台接口

```text
GET    /admin/overview
GET    /admin/providers
PUT    /admin/providers/{providerName}
GET    /admin/routes
PUT    /admin/routes/{routeName}
DELETE /admin/routes/{routeName}
PUT    /admin/providers/{providerName}/models/{modelName}
DELETE /admin/providers/{providerName}/models/{modelName}
GET    /admin/api-keys
GET    /admin/prompt-templates
GET    /admin/cache
DELETE /admin/cache
GET    /admin/models/health
POST   /admin/models/probe
GET    /admin/reports/cost
GET    /admin/reports/cost/daily
DELETE /admin/reports/cost
GET    /admin/reports/performance
GET    /admin/reports/performance/daily
DELETE /admin/reports/performance
GET    /admin/eval
PUT    /admin/eval/prompt-versions
PUT    /admin/eval/retrieval-strategies
PUT    /admin/eval/golden-dataset
POST   /admin/eval/regression-runs
POST   /admin/observability/phoenix/traces
GET    /admin/compliance
GET    /admin/compliance/reviews
GET    /admin/compliance/reviews/{reviewId}
POST   /admin/compliance/reviews/{reviewId}/confirm
GET    /admin/compliance/audit-logs
```

## 配置热更新

运行时新增模型：

```http
PUT http://localhost:8080/admin/providers/deepseek/models/deepseek-test
Content-Type: application/json

{
  "upstreamModel": "deepseek-v4-flash",
  "inputPricePer1k": 0.0,
  "outputPricePer1k": 0.0
}
```

运行时新增路由：

```http
PUT http://localhost:8080/admin/routes/deepseek-test
Content-Type: application/json

{
  "primary": "deepseek:deepseek-test",
  "fallbacks": [
    "deepseek:deepseek-v4-flash"
  ]
}
```

当前热更新修改的是运行期内存配置。生产环境建议进一步接入数据库、Nacos、Apollo 或配置中心。

## 代理配置

如果浏览器能访问模型 API，但 Java WebClient 连接超时，可以给 JVM 或环境变量配置代理。

VM options 示例：

```text
-Dhttps.proxyHost=127.0.0.1 -Dhttps.proxyPort=7897
```

环境变量示例：

```powershell
$env:HTTPS_PROXY_HOST="127.0.0.1"
$env:HTTPS_PROXY_PORT="7897"
```

## HTTP 调试

完整调试样例见：

```text
http/chat.http
```

可以直接在 IntelliJ IDEA 中打开该文件，点击每个请求左侧的绿色运行按钮。

## 上传 GitHub 前注意

项目已提供 `.gitignore`，会忽略：

```text
target/
logs/
.env
.env.*
application-local.yml
application-secret.yml
.idea/
*.iml
*.log
```

上传前仍建议执行：

```powershell
git status
```

确认没有真实 API Key、日志、编译产物被加入版本库。

## 常用命令

```powershell
mvn test
mvn clean package -DskipTests
java -jar target\llm-gateway-0.1.0.jar
```

Git 初始化：

```powershell
git init
git add .
git commit -m "Initial commit: Java WebFlux LLM Gateway"
git branch -M main
git remote add origin https://github.com/你的用户名/llm-gateway.git
git push -u origin main
```

## 前端控制台

前端位于 `frontend/`，使用 Vue 3 + TypeScript + Vite + Element Plus + ECharts。

启动方式：

```bash
cd frontend
npm install
npm run dev -- --port 5173
```

访问地址：

```text
http://localhost:5173
```

页面模块：

- Dashboard：Provider、路由、缓存、成本和性能概览。
- 模型治理：Provider、协议、baseUrl、模型列表和健康探测。
- 路由策略：primary、weighted、canary、fallback 热更新。
- Prompt 治理：Prompt 模板、变量示例、bad case 和工程治理资产。
- Playground：普通聊天、SSE 流式输出、RAG 增强聊天和 Agent 轨迹预览。
- 评估中心：Golden Dataset、Ragas 风格指标和回归记录。
- 调用观测：成本报表、性能报表和工程治理快照。
- 连接设置：Admin Basic Auth 和可选 `X-Api-Key`。

默认前端通过 Vite proxy 转发 `/admin`、`/v1`、`/actuator` 到 `http://localhost:8080`，因此需要先启动 Spring Boot 后端。

## 面试题库

项目配套的 86 道面试题及详细答案位于 [LLM Gateway 面试题详解](docs/interview/00-LLM-Gateway面试题详解-总目录.md)。题库按总体设计、WebFlux、SSE、多模型路由、可靠性、Redis、Token 成本、安全、RAG/Agent 和综合场景拆分，并明确区分当前实现、演示边界和生产化方案。

成本治理采用“条件分位数预测预留 -> 厂商 usage 实际结算 -> 账单对账”的分层设计，详细实现、官方价格来源和仍需补齐的财务级能力见 [成本预测与结算](docs/成本预测与结算.md)。
