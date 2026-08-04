# 七服务联调与架构审计

## 联调结论

入口链路为 `llm-gateway → agent-runtime → control-plane/context/rag/tool → governance`。
Runtime 只消费 Control Plane 发布快照，Context Service 负责历史消息和证据组织，Tool Gateway 是工具执行的唯一边界，LLM Gateway 是模型调用的唯一边界，Governance 只消费事件和评测/审计数据。

本次扫描确认生产 Compose 中的核心服务名、Tool Gateway 工具 URL、RAG 内部鉴权 Header 和 Runtime 的工具版本校验一致。`controlled_scan` 通过 Tool Gateway 目录注册，不允许绕过权限直接读文件系统。

## 有意保留的兼容代码

- `rag-agent-service/app/services.py` 是旧同步应用入口；`app/runtime/container.py` 是分布式 Runtime 入口。二者不能直接合并，否则会破坏离线审查 API 和生产 Temporal 入口。
- `AgentHarness.agent_graph` 是迁移兼容别名，业务代码应使用 `agent_harness`；待下游全部迁移后才能删除。
- SQLite 适配器是本地开发/测试实现，生产配置通过校验强制 PostgreSQL，不能简单删除。

## 架构优化空间

1. 将 `platform-contracts` 的事件和工具 Schema 接入 CI Schema Registry，避免 Python/Java/Compose 漂移。
2. 为 Control Plane 增加 Tool Catalog 版本快照校验，发布时提前发现工具不存在或字段漂移。
3. 将 Runtime 的工具权限从请求 Header 逐步迁移到 OIDC 权限声明和发布快照能力集合。
4. 为 `controlled_scan` 增加扫描结果脱敏和审计事件关联，禁止把源码/日志原文无界传入 Prompt。
5. 为跨区域 Temporal 路由补充区域级 Worker Queue、故障转移和对账演练；当前代码支持目标选择，集群高可用仍由部署层提供。
6. 统一各工程 Ruff/格式规则。当前全量 Ruff 存在历史遗留告警，但本次新增和修改文件已通过 Ruff 检查。

## 验证结果

- rag-agent-service：103 passed
- agent-control-plane：10 passed
- agent-governance：10 passed
- tool-gateway：18 passed
- platform-infra：3 passed
- llm-gateway：Maven 测试通过
- Python 全工程 `compileall` 通过
- Compose YAML 解析通过，20 个服务、7 个 Secret，依赖引用无缺失
