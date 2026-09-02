# Agent Context Service

Context Service 负责把历史消息、记忆、知识 Evidence 和本轮任务事实组织成一个有上限、可解释、可追踪的
`ContextPackage`。它回答“模型这一轮可以看到什么”，不回答“Agent 下一步应该做什么”。

## 服务边界

| 负责 | 不负责 |
| --- | --- |
| 会话消息的租户/用户/Session 隔离 | Runtime Run 状态和 LangGraph Checkpoint |
| History、RAG、Token Budget Pipeline | Planner、Decision Engine 和 Agent Loop |
| 角色、时间、相关性和来源可信度排序 | 直接调用模型或工具 |
| RAG 可选依赖和 memory-only 降级 | 文档摄取与索引构建 |
| Task Artifact 元数据、版本链、预览和比较 | 业务文件任意读写 |

Runtime Session Ledger 记录“过去真实发生了什么”；Context Service 保存原始会话消息并决定“下一轮模型看到
什么”。两者通过 Session/Run/Trace 标识关联，但不能互相复制成第二份敏感数据仓库。

## Context 组装链路

```text
ContextAssembleRequest
  → Verified Tenant / User / Session
  → History Transformer
  → RAG Transformer
  → Role / Recency / Relevance / Source Trust Ranking
  → Token Budget Allocator
  → Prompt Injection Segmentation / Redaction / Length Limit
  → ContextPackage + Budget Report + Degradation Metadata
```

History 不再只按插入顺序拼接。系统为消息和 Evidence 计算可解释分数，并保留选择/丢弃原因；Token Budget
分别分配给系统约束、近期对话、长期记忆、知识证据和安全余量。预算不足时按策略裁剪，不会静默超过模型窗口。

## RAG 失败语义

- `required=true`：RAG 失败导致 Context 组装失败，Runtime 不得在缺失必要知识时继续回答；
- `required=false`：保留历史消息并返回显式 `memory-only` 标记、降级原因和预算报告；
- RAG 成功但无命中：返回空 Evidence 事实，不伪装成服务故障；
- 历史为空：仍生成合法 ContextPackage，不伪造用户消息。

Runtime 必须把历史消息与 Evidence 一起进入 Planning 和 Decision Prompt。Context 返回了消息但 Runtime 未使用，
属于集成缺陷，不能用“API 已返回”代替端到端完成。

## Task Artifact

Task Artifact 是大结果的不可变引用，不是把大文本塞入 Agent 消息。相同
`tenant + root_task_id + logical_name` 通过 CAS 生成单调版本；在线预览只允许批准的文本媒体类型和受限字节前缀，
版本比较只接受同一逻辑序列。下载使用短期签名 URL，数据库只保存引用、摘要、媒体类型、版本和来源关系。

在 Multi-Agent 场景中，主管和专家通过 Artifact ID 交换报告、表格或代码扫描结果，避免复制大对象，也避免
子 Agent 把完整私有 Context 回传父 Agent。

## 主要 API

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `POST` | `/context/assemble` | 生成 ContextPackage |
| `POST/GET/DELETE` | `/context/sessions/{session_id}/messages` | 追加、读取或删除会话消息 |
| `POST/GET` | `/context/tasks/{root_task_id}/artifacts` | 创建或列出 Task Artifact |
| `GET` | `/context/tasks/{root_task_id}/artifacts/{artifact_id}` | 读取 Artifact 元数据 |
| `GET` | `/context/tasks/{root_task_id}/artifacts/{artifact_id}/download-url` | 获取短期下载链接 |

实际 API 前缀以部署配置和 OpenAPI 为准。

## 安全、生命周期和部署

- 生产身份来自已验证 OIDC 声明和 mTLS 工作负载，不直接信任客户端 Header；
- 消息、Artifact 与缓存必须以 Tenant/User/Session 或 Root Task 复合键隔离；
- PostgreSQL 是消息与 Artifact 元数据真源，Redis 只能作为缓存；
- 原始 Artifact 正文进入对象存储并执行 KMS、保留期和删除策略；
- 模型可见投影要脱敏、限长，并保留原文 SHA-256 以便审计比对；
- Context 服务故障不能让 Runtime 回退为“无限制直接拼 Prompt”。

工程入口位于 `apps/agent_context_service`，核心 Pipeline 位于 `app/context/`，接口位于
`app/service_api/context_api.py`。整体部署和身份要求见[平台部署指南](../../../docs/deployment-guide.md)。
