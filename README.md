# Agent Platform

面向企业 RAG Agent 的七服务平台。仓库采用 monorepo 管理共享契约与本地联调配置，但各服务保持独立进程和部署边界。

## 七个逻辑服务

| 服务 | 代码位置 | 默认端口 |
| --- | --- | --- |
| Agent Control Plane | `agent-control-plane/` | 8082（容器内 8080） |
| Agent Runtime | `rag-agent-service/apps/agent_runtime/` | 8001 |
| Agent Context Service | `rag-agent-service/apps/agent_context_service/` | 8002 |
| RAG Service | `rag-agent-service/apps/rag_query_api/`、`apps/ingestion_api/`、`apps/ingestion_worker/` | 8003、8004、Worker |
| LLM Gateway | `llm-gateway/` | 8080 |
| Tool Gateway | `tool-gateway/` | 8090 |
| Agent Governance | `agent-governance/` | 8081 |

RAG 在线查询与离线知识加工相互分离：

- `rag-query-api` 处理在线检索、召回和证据返回。
- `ingestion-api` 接收解析、OCR、重建索引等任务。
- `ingestion-worker` 异步执行知识加工任务。

## 公共能力

- `platform-contracts/`：执行上下文、错误 Envelope、治理事件 JSON Schema。
- `compose.platform.yaml`：七个逻辑服务及 MySQL、Redis 的本地完整拓扑。
- `scripts/platform_e2e.py`：控制面发布、Runtime、RAG、Tool、Governance 黑盒联调门禁。

## 本地启动

```powershell
docker compose -f compose.platform.yaml up --build -d
```

启动完成后执行全链路验证：

```powershell
python scripts/platform_e2e.py
```

本地 `.env`、数据库、日志、虚拟环境和构建产物不会提交到仓库。部署前请从各服务的 `.env.example` 创建本地配置，并替换示例密钥。
