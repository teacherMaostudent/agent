# Model Lab

Model Lab 是独立的**离线模型实验服务**，与线上 Agent Runtime、Agent Lab 明确分离。它登记
LoRA/QLoRA、DPO/GRPO 和分布式训练的实验计划、评测结果和模型卡；通过评测的模型工件可以提交给
Control Plane，作为 Ollama/vLLM 路由发布的候选。

它研究“模型是否可用”，而 Agent Lab 研究“某个 Prompt、Graph、RAG、Tool、Planner 与权限组合的
Agent 是否可用”。模型卡不能替代 Agent 的端到端回放；反之，Agent 回放也不能证明模型训练过程
可复现。

## 支持的计划

- `lora` / `qlora`：监督微调计划；
- `dpo` / `grpo`：偏好/对齐训练计划；
- `distributed`：DeepSpeed、FSDP、TorchRun 等 Worker 启动元数据。

每个计划都必须固定数据集指纹、基础模型 revision、随机种子、容器镜像 digest 和评测阈值。只有
提交的评测通过阈值，才会生成模型卡。

## 生产边界

本服务不把 GPU 框架打进平台 API 镜像。生产 API 使用 PostgreSQL 保存冻结计划、Worker 身份、评测
结果与模型卡；仅接受写入已配置对象存储桶的工件 URI。独立 GPU Worker 应使用固定 digest 的训练/评测
镜像（Kubernetes Job 或 Temporal Activity），调用内部 `begin` 与 `results` 接口回传 manifest。API、
Control Plane 与 Worker 使用 OIDC、mTLS 和服务密钥；内存字典与匿名 `evaluate` 接口已移除。
