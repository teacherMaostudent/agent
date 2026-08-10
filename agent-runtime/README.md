# Agent Runtime

`agent-runtime` 是独立部署的执行平面：加载发布快照、运行 Planner/LangGraph/Harness、管理预算与审批、驱动 Temporal Worker，并通过 HTTP 调用 Context、RAG、LLM Gateway 和 Tool Gateway。

当前处于兼容迁移阶段：实现仍复用已验证的 `app.agent` 与 `app.runtime` Python 包，以保证没有第二套状态机；本镜像不启动 RAG Query 或摄取进程。Runtime 与 RAG 的数据交互仅使用 `/api/v1/query` 契约。待所有调用方切换后，再将内部包物理迁入本项目。
