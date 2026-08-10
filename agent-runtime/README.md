# Agent Runtime

`agent-runtime` 是平台的独立执行平面：它加载控制面已经发布的不可变快照，执行 Planner、LangGraph 和 Harness，并保存运行状态、预算、审批中断及异步任务。

它不读取 RAG、Context 或摄取服务的内部仓储、数据库和 Python 包。历史消息通过 Context HTTP API 获取；知识证据通过 RAG HTTP API 获取；模型与工具分别通过 LLM Gateway、Tool Gateway 调用。这样每个服务的 ACL、数据生命周期与工作负载身份边界都可以独立部署和审计。

## 运行职责

- 将用户请求与发布快照编译为受限的执行计划；
- 在 LangGraph 中执行受预算、可取消、可恢复的 Agent Loop；
- 将高风险工具调用转换为审批中断，审批恢复后继续同一运行；
- 将运行完成事件写入本地事务 Outbox，由治理链路异步投递；
- 在启用 Temporal 时作为 Worker 消费跨区域任务队列。

## 镜像依赖

镜像仅安装 `agent-platform-infra`、`agent-platform-sdk` 与 Runtime 自身。SDK 提供版本化契约、服务客户端、统一服务 Web 边界和脱敏规则；Infra 提供 OIDC/mTLS、OPA、Tracing 等基础设施适配器。Runtime Dockerfile 不再复制或安装 `rag-agent-service`。

## 本地验证

在仓库根目录执行：

```powershell
$env:PYTHONPATH="$PWD\platform-sdk;$PWD\platform-infra;$PWD\agent-runtime\src"
python -m pytest agent-runtime/tests -q
```

生产 Compose 为 Runtime、Context、RAG Query、摄取 API 和摄取 Worker 分配不同的 OIDC 工作负载客户端。部署时还应为每个工作负载签发独立 mTLS 证书；`X-Tenant-Id` 等 Header 只保留为迁移期兼容输入，不能作为生产身份信任根。
