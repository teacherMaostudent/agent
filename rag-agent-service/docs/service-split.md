# RAG、Context 与摄取服务拆分

本目录提供三个可独立部署的 API 和一个 Worker。Agent Runtime 已迁出到仓库根目录的
`agent-runtime/`，不再属于 RAG 应用包。工具执行委托给独立的 `tool-gateway`；共享 Python
包只提供契约和基础能力，绝不共享进程内状态。

## Service boundaries

| Process | Entrypoint | Port | Owns |
| --- | --- | --- | --- |
| agent-context-service | `apps.agent_context_service.main:app` | 8002 | sessions, memory, token budget and context assembly |
| rag-query-api | `apps.rag_query_api.main:app` | 8003 | ACL filtering, hybrid retrieval and reranking |
| knowledge-ingestion-api | `apps.ingestion_api.main:app` | 8004 | uploads and ingestion job submission |
| ingestion-worker | `apps.ingestion_worker.main` | n/a | parsing, OCR and index builds |
| tool-gateway | `../tool-gateway/app.main:app` | 8090 | discovery, validation, authorization, approval, resilience and audit |

Runtime 的唯一线上入口是 `agent-runtime/src/agent_runtime_service.main:app`（端口 8001）。
RAG 代码树中不存在兼容 Runtime 入口；新部署不得将 RAG Pod 同时作为 Runtime 使用。

## Local startup

Create `.env` from `.env.example`, use `RAG_PERSISTENCE=sqlite`, then start:

```powershell
uvicorn apps.rag_query_api.main:app --port 8003
uvicorn apps.agent_context_service.main:app --port 8002
uvicorn apps.ingestion_api.main:app --port 8004
python -m apps.ingestion_worker.main
# In ../tool-gateway:
uvicorn app.main:app --port 8090
```

Or run:

```powershell
docker compose -f ..\compose.platform.yaml up --build
```

Run the repeatable HTTP smoke test with:

```powershell
python -m scripts.distributed_smoke
```

## Online request path

```text
client
  -> agent-runtime（独立服务）
  -> agent-context-service / rag-query-api（HTTP 契约）
  -> llm-gateway（模型调用）
  -> tool-gateway -> HTTP API / MCP Server（模型选择受控工具时）
```

Runtime 通过 Context Service 保存和读取历史消息，并直接使用 RAG Query API 获取证据。Context
负责 Token Budget 与排序；RAG Query 在召回/重排前过滤租户、用户 ACL。Runtime 不得 import
它们的仓储、检索器、切块器或索引实现。

## Offline ingestion path

```text
upload -> ingestion-api -> durable job -> ingestion-worker
       -> parse/OCR/index -> metadata repository/index
       -> rag-query-api reads the published data
```

The included SQLite queue supports local development and single-host rollout.
For multiple worker replicas, replace `IngestionJobStore` with Kafka/Celery and
replace `SqliteRepository` with PostgreSQL plus a production search index. The
API and processor contracts do not need to change.

## Compatibility and migration

- New uploads use `/api/v1/ingestion/documents` and return HTTP 202 with a job.
- Poll `/api/v1/ingestion/jobs/{job_id}` for completion.
- Online search uses `/api/v1/query/search`.
- Internal calls use `RAG_INTERNAL_SERVICE_API_KEY` when service auth is enabled.
- Runtime 使用自身的 Tool Gateway 服务凭证，永远不会接触 Tool Gateway 管理员凭证。
- Every session is namespaced by `tenant_id:user_id:session_id`.
