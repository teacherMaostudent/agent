# 桌面 Agent 产品与 Harness Benchmark 指南

## 目标

`agent-desktop` 把现有企业执行平台收敛成真实用户可操作的产品入口。它不创建第二套
Planner、Graph 或审批状态机，而是通过 `POST /api/v1/agent/interactive-runs` 提交任务，随后读取
Run Snapshot 与 Session Ledger。交互入口在返回 `run_id` 前冻结 Release/Snapshot；本地模式
进入 SQLite 持久队列，生产模式进入 Temporal，Worker 最终仍复用唯一 `run_agent` 实现。

## 安全边界

1. Electron 使用 `contextIsolation=true`、`sandbox=true`、`nodeIntegration=false`；Renderer
   不能直接访问文件系统、网络凭据或 Node API。
2. Runtime 地址、OIDC Token 和租户身份由 Electron 主进程持有。Token 不进入 React 状态、
   Local Storage 或反馈文件。
3. 用户选择本地目录后，主进程只生成深度不超过 3、最多 250 项的相对路径清单，并跳过
   `.git`、`.venv`、`node_modules` 和构建目录；不会自动上传文件正文。
4. `controlled_scan` 只接受服务端部署管理员配置的 scope。Renderer 选择目录不会自动升级为
   RAG/Tool Gateway 的文件读取权限。
5. 工具审批、参数 Schema、权限、幂等、限流和审计仍属于 Tool Gateway；桌面端只提交人工决定。

## 三个演示任务

### 源码与日志扫描

部署端将显式目录以只读方式挂载到 RAG Query 工作负载，并配置：

```text
RAG_SCAN_ROOTS={"workspace":"/workspace"}
```

目标 Agent 的发布快照必须精确绑定 `controlled_scan@1.0.0` 和 `file:scan` 权限。演示时输入
TODO、异常类型或疑似密钥模式，界面应显示 Plan、工具意图、工具结果和带文件行号的回答。

### 证据型研究报告

先通过 Ingestion API 摄取文档并冻结索引版本。任务要求 Agent 区分事实、推断和未知项，输出
Evidence ID。演示重点是 Context/RAG 证据链、引用正确性和证据不足时的明确降级。

### 工作区整理预案

桌面端只把有界文件清单作为不可信业务上下文提交，Agent 输出拟移动项、冲突和回滚方案。
当前版本故意不自动执行移动：在新增可恢复、幂等且进入系统回收站的写工具前，文件修改必须
保持 fail-closed。这比为了 Demo 在 Renderer 里直接调用 `fs.rename` 更符合平台边界。

## Harness Benchmark

使用 `agent-lab/examples/desktop-harness-benchmark.json` 创建第一组实验，将直接 ReAct 发布版本
作为 baseline。再创建相同用例的 Plan-Execute、Context 压缩和失败恢复候选实验，并填写
`baseline_experiment_id`。Agent Lab 从 Runtime Ledger 计算：

- Task Success Rate；
- 工具、模型和检索调用数；
- 人工审批率和恢复事件数；
- 权限违规次数；
- Evidence Recall 和 Tool Selection Precision；
- 平均延迟、已知 USD 成本与 Governance Quality Gate。

最终答案质量由冻结 Judge/Rubric 评定，轨迹效率由上述确定性指标评定。不能用 Judge 平均分
代替工具选择、权限违规或检索召回。

## 用户反馈闭环

任务终态允许选择“有帮助/需改进”并填写原因。反馈保存在 Electron 用户数据目录的
`feedback/feedback.jsonl`，写盘前自动删除 API Key、Bearer Token 和 Windows 绝对路径。
建议每周执行一次分层复核：优先抽取负反馈、高风险工具、人工接管和新知识域样本；人工确认后
把问题转成 Replay Case，而不是直接把未经审核的用户文本加入 Golden Dataset。

## 投递演示验收

- 两分钟内完成安装、连接和一次任务；
- 视频同时展示计划、工具调用、审批/取消、最终结果和 Trace；
- README 明确哪些能力已经实现，哪些仍需部署配置；
- Benchmark 报告至少包含 30 个任务和一个 baseline，不伪造真实用户数量；
- 用户反馈必须经过人工复核后才进入评测集。
