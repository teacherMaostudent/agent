# Model Lab

Model Lab 是独立的**离线模型实验服务**，与线上 Agent Runtime、Agent Lab 明确分离。它登记
LoRA/QLoRA、DPO/GRPO 和分布式训练的实验计划、评测结果和模型卡；通过评测的模型工件可以提交给
Control Plane，作为 Ollama/vLLM 路由发布的候选。

它研究“模型是否可用”，而 Agent Lab 研究“某个 Prompt、Graph、RAG、Tool、Planner 与权限组合的
Agent 是否可用”。模型卡不能替代 Agent 的端到端回放；反之，Agent 回放也不能证明模型训练过程
可复现。

## 为什么需要独立 Model Lab

平台主要调用 GPT、Claude、DeepSeek、Kimi、通义等托管 API，拿不到权重时不能用 PyTorch 对这些闭源模型
执行 LoRA、QLoRA 或 DPO。Model Lab 的价值首先是管理可自部署开源模型和离线模型实验，并为 Control Plane
提供可验证模型工件；对于闭源模型，它可以保存固定 revision 的基准评测和模型卡，但不会伪装成训练服务。

训练 Worker 与平台 API 分离，是为了不把 CUDA、PyTorch、Transformers、PEFT、TRL 和分布式运行时打进每个
在线服务镜像。GPU 节点可以独立扩缩容和隔离数据，训练失败也不会影响 Runtime、Gateway 或用户请求。

## 模型实验生命周期

```text
Frozen ExperimentPlan
  → Worker Claim / Begin
  → Fixed Image Digest + Dataset Fingerprint + Base Revision + Seed
  → Train / Quantize / Evaluate
  → Artifact Manifest in Object Storage
  → Metrics + Model Card
  → Approved / Rejected
  → Control Plane Model Route Candidate
  → LLM Gateway Canary / Promote / Rollback
```

Model Lab 不直接向 LLM Gateway 写路由。Control Plane 校验实验、模型卡、工件摘要、评测阈值和 Governance
证据后才创建候选路由；Gateway 只执行已发布的模型版本。

## 支持的计划

- `lora` / `qlora`：监督微调计划；
- `dpo` / `grpo`：偏好/对齐训练计划；
- `distributed`：DeepSpeed、FSDP、TorchRun 等 Worker 启动元数据。

每个计划都必须固定数据集指纹、基础模型 revision、随机种子、容器镜像 digest 和评测阈值。只有
提交的评测通过阈值，才会生成模型卡。

量化实验同样需要记录基础权重、量化算法、位宽、校准数据指纹、运行硬件和精度/时延变化，不能只上传一个
文件名。闭源 API 模型只能执行固定 revision 的外部基准，不能产生伪造的本地权重工件。

## 与 Multi-Agent 的关系

Multi-Agent 中主管、专家、Judge 和轻量分类器可能需要不同模型。Model Lab 为这些角色提供候选模型的质量、
延迟、成本、显存和许可证证据；Capability/LLM Router 再按已发布策略选择。它不决定 Agent 拓扑，也不管理
父子会话。

例如高风险专家可绑定经过严格评测的大模型，意图分类或 Reranker 可绑定小型自部署模型，离线 Judge 必须固定
并校准自己的 revision。多模型组合最终仍需 Agent Lab 做端到端回放。

## API 与责任主体

| 方法 | 路径 | 调用方与用途 |
| --- | --- | --- |
| `POST` | `/v1/experiments` | 实验负责人登记冻结计划 |
| `POST` | `/internal/v1/experiments/{id}/begin` | 已认证 GPU Worker 领取并记录身份 |
| `POST` | `/internal/v1/experiments/{id}/results` | Worker 回传工件 Manifest 与指标 |
| `GET` | `/internal/v1/experiments/{id}` | Control Plane/Worker 查询不可变记录 |

内部接口要求工作负载身份、mTLS 和服务凭据；浏览器和普通 Runtime 不应访问。

## 生产边界

本服务不把 GPU 框架打进平台 API 镜像。生产 API 使用 PostgreSQL 保存冻结计划、Worker 身份、评测
结果与模型卡；仅接受写入已配置对象存储桶的工件 URI。独立 GPU Worker 应使用固定 digest 的训练/评测
镜像（Kubernetes Job 或 Temporal Activity），调用内部 `begin` 与 `results` 接口回传 manifest。API、
Control Plane 与 Worker 使用 OIDC、mTLS 和服务密钥；内存字典与匿名 `evaluate` 接口已移除。

真实训练能力取决于独立 GPU Worker、固定容器镜像、数据集访问授权、对象存储、调度平台和算力。仓库中的
Model Lab API 完成实验控制与证据边界，但不能把没有部署 GPU/Kubernetes/Temporal Worker 的环境描述成已经
完成大规模训练平台。
