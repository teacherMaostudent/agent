# 线上评估与 Golden 闭环

> 迁移说明：Trace 由 Gateway 异步发送到 `agent-governance`，采样、Judge、
> 人工审核与 Golden 发布状态全部由 Governance 持有。

## 1. 自动化边界

| 工作 | 当前实现 | 人工边界 |
|---|---|---|
| 收集业务请求与 Trace | 进入 `ChatGatewayService` 的模型请求 100% 发布最终事件；缓存、流式、fallback 和最终异常均覆盖 | 不需要 |
| Schema、格式、计算校验 | 每条 Trace 自动检查请求结构、回答格式、JSON 合约和 usage 加总 | 通常不需要 |
| 超时、异常、成本检测 | 按阈值和最终异常自动产生规则信号 | 不需要 |
| 用户点赞/点踩 | Playground 自动生成 `X-Request-Id`，反馈先落库再异步评估 | 不需要 |
| LLM Judge | 规则失败、异常、点踩 100% 触发；普通成功请求按比例抽样 | 需要定期抽检 |
| Bad Case 初步分类 | 规则信号、反馈和 Judge 结果自动分类 | 高风险审核 |
| 根因定位 | 自动形成分类、假设、证据与定位方法 | 复杂问题人工修正 |
| Golden 候选 | 高置信度自动生成；中置信度经人工确认后生成 | 必须审核 |
| 标准答案确认 | 系统只生成候选，不自动发布 | 必须人工 |
| 高风险 Golden | 状态固定为 `PENDING_EXPERT_APPROVAL` | `expertApproval=true` 才可发布 |
| 普通 Golden | 状态为 `PENDING_REVIEW` | 人工确认并定期抽检 |

这里的“100% Trace”指已经被 WebFlux 成功解析并进入网关业务入口的模型请求。代理层拒绝、TLS 失败或
无法解析的畸形 HTTP 请求应由 Nginx/API Gateway Access Log 和 OpenTelemetry 补齐，不能由 Controller
内部代码承诺采集。

## 2. 事件链路

```text
/v1/chat/completions
  -> ChatGatewayService.complete / stream
  -> 缓存、路由、fallback、usage 结算
  -> GatewayTraceEvent（最终结果只发布一次）
  -> OnlineEvaluationGovernanceService.collectTrace
  -> 保存 eval-online-sample
  -> 自动 Phoenix Trace
  -> RuleValidation
       request.messages / response format / JSON contract
       usage 计算 / timeout / exception / cost anomaly
  -> 是否触发 Judge
       规则失败、异常、点踩：是
       普通请求：normal-sample-rate 抽样
  -> LlmJudgeService.evaluateOnline
  -> 失败置信度计算
       HIGH   -> AUTO_BAD_CASE -> 根因 -> Golden 候选
       MEDIUM -> HUMAN_REVIEW
       LOW    -> SAMPLE_POOL
```

`internal-evaluation` 租户被明确排除，因此 Judge 和 Golden 生成模型调用不会再次触发线上评估，
避免递归调用。

## 3. 规则校验

`RuleValidation` 保存：

- `schemaValid`：请求必须是对象，`messages` 必须为非空数组。
- `formatValid`：成功响应必须有回答；声明 JSON Schema 时回答必须能解析成 JSON。
- `calculationValid`：Token 不得为负，`total_tokens = prompt_tokens + completion_tokens`。
- `timeout`：延迟超过 `timeout-threshold`，或异常文本包含 timeout。
- `exception`：所有路由最终失败。
- `costAnomaly`：单次成本超过配置阈值。
- `errors`：保留每个失败原因，供根因分析和审计使用。

规则全部由 Java 执行，不依赖 LLM，因此结果确定、成本为零。

## 4. 失败置信度与分流

置信度信号包括异常、Schema/格式/计算失败、超时、成本异常、用户点踩、Judge 低分和 Judge 未通过。
各信号累加后截断到 1.00：

- `>= 0.80`：高置信度，自动创建 `BadCaseRecord`。
- `>= 0.50 && < 0.80`：中置信度，进入 `HUMAN_REVIEW`。
- `< 0.50`：进入 `SAMPLE_POOL`，可通过管理接口手动 Judge。

初步分类包括 `TIMEOUT`、`UPSTREAM_ERROR`、`SCHEMA_OR_FORMAT_VIOLATION`、
`USAGE_CALCULATION_ERROR`、`COST_ANOMALY`、`HALLUCINATION`、`SAFETY_COMPLIANCE`、
`USER_REJECTION` 和 `LOW_QUALITY`。

## 5. 用户反馈

```http
POST /v1/feedback
Content-Type: application/json

{
  "requestId": "原聊天请求的 X-Request-Id",
  "rating": "DOWN",
  "reason": "遗漏关键条件",
  "expectedAnswer": "可选的用户期望答案",
  "userId": "demo"
}
```

反馈先保存为 `eval-user-feedback`，接口立即响应。点踩随后异步触发多 Judge，模型超时不会导致反馈丢失。

## 6. 人工审核状态机

中置信度样本允许三种动作：

- `CONFIRM_BAD_CASE`：确认问题，允许修正根因和期望答案，随后创建 Bad Case 与 Golden 候选。
- `DISMISS`：判定误报，状态变成 `HUMAN_DISMISSED`。
- `KEEP_SAMPLING`：暂不定性，回到普通样本池。

审核记录保存为 `eval-sample-review`，包含 reviewer、动作、根因、期望答案和备注。

## 7. Golden 候选与审批

候选答案优先级：人工审核提供的标准答案 > 用户反馈中的期望答案 > 独立模型生成。模型生成只产生候选，
不会直接写入正式 Golden Dataset。

包含 GMP、药品、医疗、法律、财务、安全、放行、CAPA 等关键词，或分类为
`SAFETY_COMPLIANCE` 的样本会标记为高风险：

```text
PENDING_EXPERT_APPROVAL
  -> APPROVE + expertApproval=true -> PUBLISHED
  -> REJECT -> REJECTED
```

普通候选从 `PENDING_REVIEW` 开始，但同样必须提供 reviewer 并执行 APPROVE，才会调用
`EvaluationService.upsertGoldenCase()` 写入正式 Golden Dataset。

## 8. 持久化类型

- `eval-online-sample`：完整请求、响应、规则、Judge、置信度、分类和流转状态。
- `eval-user-feedback`：点赞/点踩与期望答案。
- `eval-sample-review`：人工审核轨迹。
- `eval-golden-candidate`：候选答案、风险和审批状态。
- `bad-case`：高置信度或人工确认的问题复盘。
- `eval-golden-case`：正式 Golden Dataset。

MySQL 开启时统一保存到 `llm_runtime_documents`；本地演示关闭持久化时使用内存实现。
