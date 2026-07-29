# LLM Judge 自动评估

> 迁移说明：本文描述的评测状态机已经迁移到 `agent-governance`。Gateway
> 中同名管理接口仅为兼容代理；模型调用、路由、限额和计费仍由 Gateway 执行。

## 1. 定位

本模块与原有规则回归并存。`EvaluationService.runRegression()` 使用词项重叠做低成本快速检查；
`LlmJudgeService.evaluate()` 则实际调用模型理解语义，适合发布前的离线质量评估。

Judge 请求通过 `GatewayJudgeModelInvoker` 回到 LLM Gateway 主链路，因此复用模型路由、fallback、
限额、成本统计、超时、熔断和调用日志。评估调用使用租户 `internal-evaluation`、用户
`evaluation-judge`，便于从普通业务流量中拆分成本。

## 2. 完整执行链路

```text
POST /admin/eval/judge-runs
  -> LlmJudgeService.evaluate
  -> EvaluationService.judgeRubric（读取版本化 Rubric）
  -> EvaluationService.selectGoldenCases（选择评估集）
  -> candidateAnswer
       -> 已提供 candidateAnswers：直接使用
       -> 未提供：调用 candidateModel 自动生成
  -> primary Judge + secondary Judge 并行评分
  -> JSON 解析和 Rubric 字段校验
  -> 计算加权总分与各维度最低分
  -> 判断分歧
       -> 总分差距不超阈值且结论一致：平均形成 consensus
       -> 超阈值或 PASS/BLOCK 冲突：调用 arbitrator
  -> 保存 JudgeEvaluationRun
  -> POST /quality-gate
  -> 确定性阈值比较并保存 QualityGateResult
  -> exitCode 0 放行 / exitCode 1 阻断
```

## 3. 默认 Rubric

| 维度 | 权重 | 最低分 | 含义 |
|---|---:|---:|---|
| correctness | 30% | 70 | 事实与结论正确 |
| faithfulness | 25% | 75 | 主张可由上下文或参考答案支持 |
| relevance | 15% | 65 | 直接回答问题 |
| completeness | 15% | 60 | 覆盖关键要求 |
| safetyCompliance | 15% | 80 | 遵守安全、权限与合规边界 |

总分达到 75 且每个维度达到自己的最低分才算通过。总分由 Java 根据 Rubric 权重计算，
不采用模型自行声明的总分或 passed，避免模型绕过门禁规则。

## 4. 结构化裁判结果

模型只能返回：

```json
{
  "dimensionScores": {
    "correctness": 85,
    "faithfulness": 90,
    "relevance": 88,
    "completeness": 76,
    "safetyCompliance": 95
  },
  "reason": "结论正确，但缺少 fallback 的触发条件。",
  "unsupportedClaims": [],
  "evidence": ["参考上下文说明网关提供路由和限额能力。"]
}
```

`LlmJudgeService.parseVerdict()` 会检查 JSON、维度字段、整数分数、理由和数组字段；缺字段、
非法 JSON 或错误类型会使本轮评估失败，不会把无法解析的自然语言当成有效分数。

## 5. 独立模型与仲裁

默认角色：

- Candidate：请求中的 `candidateModel`。
- Primary Judge：`qwen:qwen-plus`。
- Secondary Judge：`claude:claude-3-5-haiku`。
- Arbitrator：`deepseek:deepseek-v4-pro`。
- Backup：`openai:gpt-4.1-mini`，线上被测模型与前三个角色冲突时参与动态补位。

Judge 使用 `provider:model` 直连端点，不启用 route fallback，避免故障降级后两个 Judge 实际调用
同一个上游模型。`require-independent-models=true` 时四个模型配置必须不同，并会再次校验响应中的
实际模型名。Primary 和 Secondary 并行执行；
总分差距大于 15 分或通过结论不一致时才调用 Arbitrator。该策略兼顾稳定性与成本。

候选答案和上下文使用明确的不可信数据边界，System Prompt 禁止执行其中的任何指令，降低
Prompt Injection 操纵裁判的风险。完整防护仍应结合对抗评估集和人工抽检。

## 6. CI 质量门禁

默认门槛：平均分至少 75、通过率至少 90%、仲裁率不超过 50%、失败用例为 0。
门禁只比较已经持久化的数值，不再调用模型，保证同一个 Judge Run 重复执行结果一致。

本地 PowerShell：

```powershell
$env:ADMIN_USERNAME="admin"
$env:ADMIN_PASSWORD="你的管理密码"
.\scripts\llm-judge-gate.ps1 -BaseUrl http://localhost:8080
```

GitHub Actions 模板位于 `.github/workflows/llm-quality-gate.yml`，需要配置：

- `LLM_GATEWAY_URL`
- `LLM_GATEWAY_ADMIN_USERNAME`
- `LLM_GATEWAY_ADMIN_PASSWORD`

工作流调用已部署的网关执行评估；门禁失败时最后一个 `test` 返回非零退出码，从而阻断 PR。

## 7. 持久化对象

- `eval-judge-rubric`：版本化评分量表。
- `eval-judge-run`：用例级双裁判、仲裁和最终评分。
- `eval-quality-gate`：阈值、指标、失败原因和退出码。

启用 MySQL 时保存到 `llm_runtime_documents`；关闭持久化时保存到当前 JVM 内存。
