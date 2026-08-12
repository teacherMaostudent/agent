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

本服务不把 GPU 框架打进平台 API 镜像。生产 Worker 应使用固定 digest 的实验镜像，通过 Kubernetes
Job 或 Temporal Activity 执行，把不可变工件写入对象存储，再将结果 manifest 回传。当前代码侧
保留实验元数据与模型卡主线；GPU 调度、工件仓库、权限隔离和大规模训练队列仍需按部署环境实现。
