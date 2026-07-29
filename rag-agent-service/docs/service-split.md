# RAG Service Split

The RAG repository provides four independently deployable APIs and one worker.
Agent tool execution is delegated to the adjacent, independently deployable
`tool-gateway`; shared Python packages contain contracts and domain logic, not
shared in-process state.

## Service boundaries

| Process | Entrypoint | Port | Owns |
| --- | --- | --- | --- |
| agent-runtime | `apps.agent_runtime.main:app` | 8001 | LangGraph execution and LLM decisions |
| agent-context-service | `apps.agent_context_service.main:app` | 8002 | sessions, memory, token budget and context assembly |
| rag-query-api | `apps.rag_query_api.main:app` | 8003 | ACL filtering, hybrid retrieval and reranking |
| knowledge-ingestion-api | `apps.ingestion_api.main:app` | 8004 | uploads and ingestion job submission |
| ingestion-worker | `apps.ingestion_worker.main` | n/a | parsing, OCR and index builds |
| tool-gateway | `../tool-gateway/app.main:app` | 8090 | discovery, validation, authorization, approval, resilience and audit |

`app.main:app` remains available during migration as the legacy all-in-one
entrypoint. New deployments should not use it.

## Local startup

Create `.env` from `.env.example`, use `RAG_PERSISTENCE=sqlite`, then start:

```powershell
uvicorn apps.rag_query_api.main:app --port 8003
uvicorn apps.agent_context_service.main:app --port 8002
uvicorn apps.agent_runtime.main:app --port 8001
uvicorn apps.ingestion_api.main:app --port 8004
python -m apps.ingestion_worker.main
# In ../tool-gateway:
uvicorn app.main:app --port 8090
```

Or run:

```powershell
docker compose -f compose.services.yml up --build
```

Run the repeatable HTTP smoke test with:

```powershell
python -m scripts.distributed_smoke
```

## Online request path

```text
client
  -> agent-runtime
  -> agent-context-service
  -> rag-query-api
  -> agent-context-service
  -> agent-runtime
  -> llm-gateway (when LLM is enabled)
  -> tool-gateway -> HTTP API / MCP Server (when the model selects a tool)
```

The runtime persists user and assistant messages through the context service.
The context service applies a token budget and asks the query API for evidence.
The query API filters tenant/user ACL metadata before scoring and reranking.

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

- Existing `/api/v1/*` endpoints under `app.main:app` are unchanged.
- New uploads use `/api/v1/ingestion/documents` and return HTTP 202 with a job.
- Poll `/api/v1/ingestion/jobs/{job_id}` for completion.
- Online search uses `/api/v1/query/search`.
- Internal calls use `RAG_INTERNAL_SERVICE_API_KEY` when service auth is enabled.
- Runtime uses `RAG_TOOL_GATEWAY_API_KEY`; it never receives the Tool Gateway admin key.
- Every session is namespaced by `tenant_id:user_id:session_id`.
